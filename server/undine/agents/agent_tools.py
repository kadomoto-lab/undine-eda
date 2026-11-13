"""The tool surface an agent (the in-app assistant, or an external client over MCP) can use.

One registry, two front ends: `agent.py` (tool-use loop with the active LLM connection) and `mcp.py` (MCP server for
external LLM clients). The rules live here, so they hold for both:

* Reading is free (design context, selection, checks, history).
* Writing is only possible inside a *changeset*: the working copy `<project>~<id>`. The real design is never touched.
  There is deliberately no "apply" tool; applying is a human decision in the Changes drawer.
* Every write is recorded with the actor and the reason, like any other edit.
* Tool results are data. They can contain text typed by designers; the system prompt tells the model not to obey it.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, replace

from undine.design import intent, librelane, rtl
from undine.server import live
from undine.agents import llm_context, results
from undine.checks import parasitics
from undine.store import provenance
from undine.schematics import netlist, schematic, xschem
from undine.simulation import derived, specs as specsmod, spectrum
from undine.simulation import sim
from undine.store.changeset import CID, ChangesetError, Changesets

# Optional cap on the edits an agent may make in one changeset (counted in the changeset, so a new session does not
# reset it). None by default: the changeset is reviewed before anything reaches the design.
MAX_WRITES = int(os.environ["UNDINE_AGENT_MAX_WRITES"]) if os.environ.get("UNDINE_AGENT_MAX_WRITES") else None
RESULT_CHARS = 9000      # the in-app assistant's budget per result (results.py shortens a longer one, valid JSON)
WAIT_S = 150             # how long run_checks / run_simulation wait for their jobs
SWEEP_WAIT_S = 240       # how long check_specs / sweep wait; what is still running is collected by asking again
WAIT_JOB_MAX_S = 1800.0  # the longest wait for a job a caller can ask for (run_simulation wait_s, wait_for job:<id>)


# what run_checks / extract_parasitics say of their jobs when the server stops while they run: a check ends with the
# server, as interrupted (sim.Jobs.close: only simulations are taken up by the next one), and none starts any more
CHECKS_STOPPING = ("the server is stopping, and the checks still running end with it: the next server does not take "
                   "up DRC, LVS or extraction runs (only simulations), so they show as interrupted; call {again} "
                   "again once Undine is back (a few seconds)")
CHECKS_NOT_STARTED = ("the server is stopping, so no check starts now (it would end with the server); call {again} "
                      "again once Undine is back (a few seconds)")


def stopping(srv) -> bool:
    """The server is asked to stop (a restart, Server.stopping): a tool that waits answers now with what it has, as at
    the end of its wait (its simulations go on in the next server; its jobs of other kinds end with it: Jobs.close)."""
    ev = getattr(srv, "stopping", None)
    return ev is not None and ev.is_set()


class ToolError(Exception):
    """A problem the model should see (bad arguments, not found ...); returned as an error result."""


@dataclass
class ToolCtx:
    srv: object
    project: str                      # the real project
    actor: str = "agent"
    changeset: str | None = None      # id of the open changeset of this agent session
    writes: int = 0
    reason_default: str = ""
    log: list = field(default_factory=list)
    owner: str | None = None          # write-lease owner: "turn:<id>" (in-app assistant), "mcp:<session>[:<agent>]"
    owner_kind: str = "turn"          # "turn" | "mcp" (an MCP lease lapses without writes, see live.py)
    turn: str | None = None           # the assistant turn the records belong to
    committed: list = field(default_factory=list)   # gseq of the records the last tool call wrote
    images: list = field(default_factory=list)      # PNGs the last tool call drew (render_*, plot_waveform), for MCP clients
    limit: int | None = RESULT_CHARS  # the current call's result budget in characters (call sets it; None: whole)
    reading: bool = False             # changeset is another's, read and checked by this call only (call: changeset=)

    @property
    def cs(self) -> Changesets:
        return Changesets(self.srv)

    @property
    def work(self) -> str:
        """The project the agent reads and writes: its changeset's working copy once one is open."""
        return f"{self.project}~{self.changeset}" if self.changeset else self.project

    def need_changeset(self, check: bool = False) -> str:
        """The working copy to write (check: to run its checks, also another's that this call reads)."""
        if self.reading and not check:
            raise ToolError(f"changeset {self.changeset} is another's: changeset= reads and checks it, edits go into "
                            "your own (open_changeset)")
        if not self.changeset:
            raise ToolError("no changeset is open. Call open_changeset first (title + reason); edits are made in its working copy.")
        meta = self.cs._load(self.project, self.changeset)
        if meta["status"] != "open":
            raise ToolError(f"changeset {self.changeset} is {meta['status']}; open a new one")
        return self.work


# ---------------------------------------------------------------------------------- helpers


SCALARS = ("string", "number", "integer", "boolean")


def _args(t: dict, a: dict) -> dict:
    """The arguments in the shape of the tool's schema (objects, lists and their items; a yes / no is a boolean, the
    other plain values are converted by the tools), so a wrong shape is a usage error that shows the expected one, not
    an internal error."""
    props = t["input_schema"].get("properties") or {}
    out = dict(a)
    for k, v in a.items():
        if props.get(k) is not None and v is not None:
            try:
                out[k] = _conform(props[k], v, k)
            except ToolError as e:
                raise ToolError(f"{e}: {t['name']} takes {k}: {results.shape(props[k])}") from None
    return out


def _conform(s: dict, v, path: str, lone: bool = True):
    """v in the shape of schema s, or ToolError saying how it differs. A lone plain value where a list that holds plain
    values is expected becomes that list (signals=out, objects=M5 from the shell), except as an item of a list
    ([[x, y], ...]) or where the schema takes the value itself (replace: true)."""
    alts = s.get("anyOf") or [s]
    if lone and not isinstance(v, (dict, list)) and not any(_takes(x, v) for x in alts) and \
            any(x.get("type") == "array" and any(y.get("type") in SCALARS for y in (x.get("items") or {}).get("anyOf")
                                                 or [x.get("items") or {}]) for x in alts):
        v = [v]
    if "anyOf" in s:
        for x in s["anyOf"]:
            try:
                return _conform(x, v, path, False)
            except ToolError:
                pass
        raise ToolError(f"{path} is {_kind(v)}")
    t = s.get("type")
    if t == "object":
        if not isinstance(v, dict):
            raise ToolError(f"{path} is {_kind(v)}, not an object")
        more = s.get("additionalProperties")
        subs = {k: (s.get("properties") or {}).get(k) or (more if isinstance(more, dict) else None) for k in v}
        return {k: x if subs[k] is None or x is None else _conform(subs[k], x, f"{path}.{k}") for k, x in v.items()}
    if t == "array":
        if not isinstance(v, list):
            raise ToolError(f"{path} is {_kind(v)}, not a list")
        return [_conform(s["items"], x, f"{path}[{i}]", False) for i, x in enumerate(v)] if s.get("items") else v
    if t in SCALARS and isinstance(v, (dict, list)):
        raise ToolError(f"{path} is {_kind(v)}, not a {t}")
    if t == "boolean" and isinstance(v, str):     # `undine call move=false` sends text: bool('false') would be true
        if (yes := _yes_no(v)) is None:
            raise ToolError(f"{path} is {_kind(v)}, not true or false")
        return yes
    return v


def _yes_no(v: str) -> bool | None:
    """The text 'true' / 'false' (any case, spaces around) as a boolean; None for other text."""
    return {"true": True, "false": False}.get(v.strip().lower())


def _takes(s: dict, v) -> bool:
    """Schema s takes the plain value v as it is: a string as a string, true as a boolean (and the text 'true' /
    'false', which the shell sends), a number as a number."""
    kind = "boolean" if isinstance(v, bool) else "string" if isinstance(v, str) else "number"
    return s.get("type") == kind or (kind == "number" and s.get("type") == "integer") or \
        (kind == "string" and s.get("type") == "boolean" and _yes_no(v) is not None)


def _kind(v) -> str:
    if isinstance(v, (dict, list)) or v is None:
        return "an object" if isinstance(v, dict) else "a list" if isinstance(v, list) else "null"
    return f"a string ({v[:40]!r})" if isinstance(v, str) else f"a {'boolean' if isinstance(v, bool) else 'number'} ({v!r})"


def _lib_cell(a: dict) -> tuple[str, str]:
    if not a.get("lib") or not a.get("cell"):
        raise ToolError("lib and cell are required")
    return a["lib"], a["cell"]


def _ed(c: ToolCtx, a: dict):
    lib, cell = _lib_cell(a)
    work = c.need_changeset()
    try:
        return work, lib, cell, c.srv.editor(work, lib, cell, "schematic")
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e


def _check_limit(c: ToolCtx) -> None:
    if MAX_WRITES is None or not c.changeset:
        return
    meta = c.cs._load(c.project, c.changeset)
    done = len({r.get("opid") for _, r in c.cs._all_records(meta) if str(r.get("actor", "")).startswith("agent:")})
    if done >= MAX_WRITES:
        raise ToolError(f"edit limit reached ({MAX_WRITES} operations in this changeset, UNDINE_AGENT_MAX_WRITES); "
                        "ask the user to review what exists first")


def _write(c: ToolCtx, a: dict, op: str, fn):
    with c.srv.changeset_lock:
        return _write_schematic(c, a, op, fn)


def _write_schematic(c: ToolCtx, a: dict, op: str, fn):
    work, lib, cell, ed = _ed(c, a)
    _check_limit(c)
    if not (a.get("reason") or "").strip():
        raise ToolError("reason is required: say why this change is made")
    args = {k: v for k, v in a.items() if k not in ("reason", "lib", "cell")}
    try:
        with ed.recording(op, args, c.actor, a["reason"], c.changeset):
            res = fn(ed)
    except (KeyError, ValueError) as e:
        raise ToolError(str(e).strip("'\"")) from e
    c.writes += 1
    return res


# ---------------------------------------------------------------------------------- reading tools


def get_selection(c: ToolCtx, a: dict):
    s = c.srv.selection.get(c.project)
    if not s or not s.get("selection"):
        return {"selection": [], "note": "nothing is selected in the GUI"}
    cur = None
    try:
        cur = llm_context.revision(c.srv, c.project, s["lib"], s["cell"])
    except Exception:
        pass
    return {"lib": s["lib"], "cell": s["cell"], "view": s["view"], "selection": s["selection"],
            "selected_at": s.get("t"), "stale": bool(s.get("revision") and cur and s["revision"] != cur),
            "revision": cur}


def get_design_context(c: ToolCtx, a: dict):
    """The design context (llm_context.build), the objects asked for first: {kind, id}, or a plain name (an instance,
    a code block or a net of that name)."""
    lib, cell = _lib_cell(a)
    objs = []
    for o in a.get("objects") or []:
        k = {"instance": "i", "net": "n", "wire": "w"}.get(o.get("kind"), o.get("k")) if isinstance(o, dict) else "name"
        if k not in ("name", "i", "n", "w") or not (o.get("id") if isinstance(o, dict) else o):
            raise ToolError(f"objects: {json.dumps(o)} is not an object of the schematic: give names (['M1', 'out']) or "
                            "{kind: instance|net|wire, id}")
        objs.append({"k": k, "id": str(o["id"] if isinstance(o, dict) else o)})
    try:
        text, _ = llm_context.build(c.srv, c.work, {"lib": lib, "cell": cell, "view": "schematic", "revision": None,
                                                    "selection": objs, "overview": a.get("overview", True), "asked": True})
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    return text


def _layout_cells(c: ToolCtx, work: str, lib: str, library) -> tuple[list[str], list[str]] | None:
    """(all cells, top cells) of a project library's layout as the server holds it, with what is not saved yet (a
    cell delete_cell took out, one create_cell made): its cell files have them only after a save. None for a library
    without a layout, or a read-only one (its files are what it has)."""
    if library.readonly or not (library.has_layout() or (work, lib) in getattr(c.srv, "dbs", {})):
        return None
    db = c.srv.db(work, lib)
    with db.lock:
        return (sorted(x.name for x in db.layout.each_cell() if not x.is_ghost_cell()),
                sorted(x.name for x in db.layout.top_cells() if not x.is_ghost_cell()))


def list_cells(c: ToolCtx, a: dict):
    prj = c.srv.project(c.work)
    libs = {a["lib"]: prj.library(a["lib"])} if a.get("lib") else {n: l for n, l in prj.libraries().items() if l.origin == "project"}
    return {n: [{"cell": x["name"], "views": x["views"]} for x in l.cells(_layout_cells(c, c.work, n, l))]
            for n, l in libs.items()}


def list_symbols(c: ToolCtx, a: dict):
    """What can be placed in a schematic. With `ref`: that symbol's pins and default parameters."""
    prj = c.srv.project(c.work)
    if a.get("ref"):
        res = xschem.SymbolResolver(prj.symbol_paths())
        p = res.find(a["ref"], None)
        if p is None:
            raise ToolError(f"symbol {a['ref']} not found; list_symbols without ref shows what exists")
        sym = xschem.parse(p.read_text(errors="replace"))
        return {"ref": a["ref"], "pins": [{"name": pn.name, "x": pn.x, "y": pn.y} for pn in schematic.sym_pins(sym)],
                "note": "pin x / y are relative to the symbol's origin, unrotated; connect_pins needs no coordinates",
                "default_parameters": schematic.sym_template(sym)}
    q = (a.get("query") or "").lower()
    rows = [s for s in c.srv.symbols(c.work) if (not a.get("lib") or s["lib"] == a["lib"]) and q in s["ref"].lower()]
    limit = 80
    return {"symbols": [s["ref"] for s in rows[:limit]], "total": len(rows),
            **({"note": f"only the first {limit}; narrow it with query or lib"} if len(rows) > limit else {})}


def get_checks(c: ToolCtx, a: dict):
    lib, cell = _lib_cell(a)
    prj = c.srv.project(c.work)
    return llm_context._checks(c.srv, prj, lib, cell, c.work) or "no DRC/LVS result yet for this cell"


def get_history(c: ToolCtx, a: dict):
    lib, cell = _lib_cell(a)
    return llm_context._history(c.srv, c.work, lib, cell, int(a.get("limit", 15))) or "no recorded changes"


def get_lvs_report(c: ToolCtx, a: dict):
    lib, cell = _lib_cell(a)
    from undine.checks import verify
    prj = c.srv.project(c.work)
    # Netgen's runs have no database
    j = c.srv.jobs.latest(prj.path, kind="lvs", lib=lib, cell=cell, match=lambda st, d: (st.get("tool") or "klayout") == "klayout")
    if j is None:
        raise ToolError("no finished LVS run for this cell in the current design; call run_checks first")
    d = prj.path / "runs" / j["id"]
    det = verify.lvs_detail(d / "result.lvsdb")
    bad = {k: [r for r in det[k] if r["status"] != "Match"] for k in ("devices", "pins", "nets", "subcircuits")}
    ext = (d / "extracted.cir").read_text() if (d / "extracted.cir").is_file() else ""
    sch = (d / "schematic.spice").read_text() if (d / "schematic.spice").is_file() else ""
    fresh = provenance.freshness(c.srv, c.work, j)
    match = bool((j.get("summary") or {}).get("match"))
    diag = verify.lvs_diagnosis(d, cell, det) if not match and (d / "result.lvsdb").is_file() else None
    if not any(ln.strip() and not ln.lstrip().startswith("*") for ln in ext.splitlines()):
        ext += ("\n* (no circuit: KLayout kept nothing of the layout, see why)"
                if diag and diag["layout"]["circuit"] is None else "\n* (no circuit)")
    return {"job": j["id"], "match": match,
            **({k: diag[k] for k in ("why", "layout", "schematic")} if diag else {}),
            "not_matching": bad, "pin_errors": (j.get("summary") or {}).get("pin_errors", []),
            **({"unused_pins": u} if (u := (j.get("summary") or {}).get("unused_pins")) else {}),
            "outdated": fresh["changed"] if fresh["fresh"] is False else [],
            "counts": {k: len(v) for k, v in det.items() if isinstance(v, list)},
            "extracted_from_layout": ext[:2500], "reference_from_schematic": sch[:1500],
            "note": "layout names are on the left of each pair, schematic names on the right; None means no counterpart. "
                    "Parameter differences show as MatchWithWarning / Mismatch on the device. why: what does not match, "
                    "in counts (layout / schematic: devices by class, nets, pins of the top circuit) and names."}


CHECKERS = ("klayout", "magic")
FINDINGS = 12            # an ERC row's findings in run_checks' answer
DRC_MARKERS = 10         # get_drc_report's markers per rule by default


def _drc_runs(c: ToolCtx, lib: str, cell: str, checker: str | None) -> list[dict]:
    """The newest finished DRC run of the cell with each checker and deck (verify.latest_drc), in the working copy."""
    from undine.checks import verify

    if checker not in (None, "", *CHECKERS):
        raise ToolError(f"checker is {' or '.join(CHECKERS)}, not {checker!r}")
    return [st for st in verify.latest_drc(c.srv.jobs, c.srv.project(c.work).path, lib, cell)
            if not checker or (st.get("tool") or "klayout") == checker]


def _drc_report(c: ToolCtx, st: dict, rules: list[str] | None, limit: int) -> list[dict]:
    from undine.checks import magicflow, verify

    d = c.srv.project(c.work).path / "runs" / st["id"]
    if (d / "result.tsv").is_file():
        return magicflow.drc_report(d / "result.tsv", rules, limit)
    return verify.drc_report(d / "result.lyrdb", rules, limit)


def get_drc_report(c: ToolCtx, a: dict):
    """Where the violations of the cell's latest DRC runs are (checker: klayout | magic; both by default)."""
    lib, cell = _lib_cell(a)
    runs = _drc_runs(c, lib, cell, a.get("checker"))
    if not runs:
        raise ToolError(f"no finished DRC run of {lib}/{cell}" + (f" with {a['checker']}" if a.get("checker") else "")
                        + " in the current design: run_checks first")
    rules = [a["rule"]] if isinstance(a.get("rule"), str) and a["rule"] else a.get("rule") or None
    limit = max(1, min(int(a.get("limit") or DRC_MARKERS), 200))
    out = []
    for st in runs:
        rows = _drc_report(c, st, rules, limit)
        fresh = provenance.freshness(c.srv, c.work, st)
        out.append({"checker": st.get("tool") or "klayout", "deck": st.get("deck"), "job": st["id"],
                    "finished": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.get("finished") or st.get("created", 0))),
                    "outdated": fresh["fresh"] is False, **({"changed": fresh["changed"]} if fresh["fresh"] is False else {}),
                    "total": (st.get("summary") or {}).get("total"), "rules": rows})
    found = {r["rule"] for run in out for r in run["rules"]}
    return {"cell": f"{lib}/{cell}", "runs": out,
            **({"not_found": [r for r in rules if r not in found]} if rules and set(rules) - found else {}),
            "note": "markers in um: box [x1, y1, x2, y2]; edges [[x1, y1, x2, y2], [...]]: the two edges a spacing / "
                    "width rule measured; polygon [[x, y], ...]. Magic merges a violation's error tiles into one marker. "
                    "render_layout(markers: true, box around a marker) shows them on the layout"}


def get_changeset(c: ToolCtx, a: dict):
    """A changeset (yours, or any of the project's by id) as an agent verifies it: its record, its operations per cell
    and its checks; an open one says who is writing it, a closed one how it was closed (applied, reverted: when, by
    whom, the merge commit, or why it was left uncommitted; discarded) and the evidence archived then (its size, and
    whether a person pruned its waveforms); one being applied, discarded or reverted now, how far ("closing")."""
    cid = a.get("id") or c.changeset
    if not cid:
        return {"open": False, "note": "no changeset is yours in this session: open_changeset opens or continues one; "
                                       "get_changeset(id) reads any of the project's (list_changesets), also a closed one"}
    try:
        if not CID.fullmatch(str(cid)):
            raise KeyError(cid)
        g = c.cs.get(c.project, cid)
    except (KeyError, ValueError) as e:
        raise ToolError(f"no changeset {cid} in {c.project}; list_changesets shows them") from e
    out = {"open": g["status"] == "open", "id": g["id"], "status": g["status"], "title": g["title"],
           "reason": g.get("reason") or "", "by": g.get("actor"), "created": g.get("created"), "yours": cid == c.changeset}
    if g.get("closing"):                                    # being applied, discarded or reverted now: how far
        out["closing"] = g["closing"]
    if g["status"] == "open":
        if (w := _writer(c, cid)):
            out["writer"] = w
    else:
        for k in ("applied", "reverted", "discarded"):        # {t, by, commit, uncommitted, why, kept?} (api.md, Merge)
            if isinstance(g.get(k), dict):
                out[k] = {x: v for x, v in g[k].items() if x not in ("paths", "tip") and v is not None}
        if isinstance(g.get("evidence"), dict):           # "pruned": a person removed its waveforms (their hashes stay)
            out["evidence"] = {k: g["evidence"][k] for k in ("captured", "jobs", "path", "bytes", "pruned")
                               if g["evidence"].get(k) is not None}
        for k in ("retained", "cleanup_warning"):
            if g.get(k):
                out[k] = g[k]
    if (git := g.get("git")):
        out["git"] = {k: git.get(k) for k in ("branch", "base", "tip", "ref", "commits") if git.get(k) is not None}
    changes = g.get("changes") or []
    running = c.srv.batching.get(cid) if g["status"] == "open" else None
    if running is not None:          # a batch is being applied: all or nothing, so none of its records count yet
        p = c.srv.batch_progress.get(cid) or {}
        out["in_progress"] = {"what": "batch", **({"done": p["done"], "of": p["ops"]} if "ops" in p else {}),
                              **({"by": p["by"]} if p.get("by") else {}),
                              **({"for_s": round(time.time() - p["started"])} if p.get("started") else {}),
                              "note": "a batch is still being applied (all or nothing): its operations are not counted "
                                      "or listed, and it has no restore point, until it ends; do not edit the changeset "
                                      "before then (get_changeset again)"}
        changes = [{**lc, "ops": [op for op in lc["ops"] if (op.get("gseq") or 0) <= running]} for lc in changes]
        changes = [lc for lc in changes if lc["ops"]]
    if a.get("cell") and not any(_where(lc["lib"], op) == a["cell"] for lc in changes for op in lc["ops"]):
        raise ToolError(f"changeset {cid} changed nothing of {a['cell']}: give lib/cell as the answer's cells name them")
    out["operations"] = sum(1 for lc in changes for op in lc["ops"] if not a.get("cell") or _where(lc["lib"], op) == a["cell"])
    out["changes"] = _steps(changes, llm_context.layer_names(c.srv, c.work), bool(a.get("detail")), a.get("cell"))
    if g["status"] == "open" and running is None:
        made = {f"{lib}/{cell}": views for lib, cells in _created(c, cid).items() for cell, views in sorted(cells.items())}
        if made:              # every cell it created that is still there, the generated ones round a device too
            out["new_cells"] = made
            out["new_cells_note"] = (f"{len(made)} cell{'s' if len(made) != 1 else ''} this changeset created and still has "
                                     "(view per cell); every other cell was there before it (the baseline)")
    out["checks"] = g.get("verify")
    if g["status"] == "open":
        n = a.get("restore_points")
        if n in (None, ""):
            n = RESTORE_POINTS
        elif str(n).lower() == "all":
            n = None
        else:
            try:
                n = int(n)
                if n < 1:
                    raise ValueError
            except (TypeError, ValueError):
                raise ToolError("restore_points is how many to list (a whole number from 1) or 'all'") from None
        before = a.get("restore_before")
        try:
            before = int(before) if before not in (None, "") else None
        except (TypeError, ValueError):
            raise ToolError("restore_before is a restore point (an after of restore_points): those before it") from None
        pts, more, now = _restore_points(c, cid, n, before, running)
        out["restore_point_now"] = now
        out["restore_points"] = pts
        out["numbering"] = ("restore points are numbered by the changeset's records (restore_changeset(to=after)); "
                            "operations counts those in effect, so the two differ by the operations undone, the "
                            "undo / redo / restore records and the records of batches that were taken back")
        if more:
            out["restore_points_more"] = {"older": more, "how": f"get_changeset(restore_before={pts[-1]['after']}) lists "
                                          "the ones before the last listed; restore_points: 'all' lists every one"}
    return out


def _where(lib: str, op: dict) -> str:
    return f"{lib}/{op['cell']}" if op.get("cell") else lib


STEP_CELLS = 12          # cells a step lists with their effects (a sync_layout adds a cell per device size)


def _step(ops: dict[str, int], cells: dict[tuple, list], names: dict | None) -> dict:
    """What one step did, as an answer gives it: its operation ("batch" when of several kinds: ops counts them),
    `operations` when more than one, its view when it has one, and per cell ("lib/cell"; "lib/cell (view)" when the
    step changed several views) its effects counted (llm_context.summarize); a cell the step only created is listed in
    new_cells (a sync_layout's PCell variants)."""
    n = sum(ops.values())
    views = {v for _, v in cells if v}
    row: dict = {"op": next(iter(ops)) if len(ops) == 1 else "batch", **({"operations": n} if n > 1 else {}),
                 **({"ops": ops} if len(ops) > 1 else {}), **({"view": next(iter(views))} if len(views) == 1 else {})}
    shown, new = {}, []
    for (where, view), fx in cells.items():
        if fx and all(e.get("k") == "c" and e.get("do") == "add" for e in fx) and where.endswith("/" + str(fx[0].get("cell"))):
            new.append(where)
            continue
        shown[f"{where} ({view})" if len(views) > 1 and view else where] = llm_context.summarize(fx, names)
    if shown:
        row["cells"] = dict(list(shown.items())[:STEP_CELLS])
    if len(shown) > STEP_CELLS:
        row["cells_left_out"] = len(shown) - STEP_CELLS
    if new:
        row["new_cells"] = new[:STEP_CELLS] + ([f"… {len(new) - STEP_CELLS} more"] if len(new) > STEP_CELLS else [])
    return row


def _steps(changes: list[dict], names: dict | None = None, detail: bool = False, cell: str | None = None) -> list[dict]:
    """A changeset's operations ({lib, ops} per library, Changesets.changes) as the steps they were made in, in order
    across its libraries: a tool call (a batch is one) or an edit, with who made it, why, and what it changed per cell
    (_step). Consecutive steps alike, the same kinds of operation by the same actor for the same reason (add_box called
    twenty times for one wire), are one entry (steps: n). detail: each operation on its own; cell ("lib/cell"): only
    that cell's operations (a library's own, create_library, is the library's)."""
    ops = [(lc["lib"], op) for lc in changes for op in lc["ops"] if not cell or _where(lc["lib"], op) == cell]
    if ops and all(op.get("gseq") is not None for _, op in ops):
        ops.sort(key=lambda x: x[1]["gseq"])
    steps: list[dict] = []
    for lib, op in ops:
        key = ((lib, op.get("seq")) if detail or not op.get("opid") else op["opid"], op.get("actor"), op.get("reason"))
        if not steps or steps[-1]["key"] != key:                # a batch's ops with reasons of their own: one each
            steps.append({"key": key, "by": op.get("actor"), "reason": op.get("reason"), "ops": {}, "cells": {}, "n": 1})
        st = steps[-1]
        st["ops"][op["op"]] = st["ops"].get(op["op"], 0) + 1
        st["cells"].setdefault((_where(lib, op), op.get("view")), []).extend(op.get("effects") or [])
    merged: list[dict] = []
    for st in steps:
        prev = merged[-1] if merged else None
        alike = prev is not None and (set(prev["ops"]), prev["by"], prev["reason"]) == (set(st["ops"]), st["by"], st["reason"])
        if not detail and alike:
            for o, n in st["ops"].items():
                prev["ops"][o] += n
            for w, fx in st["cells"].items():
                prev["cells"].setdefault(w, []).extend(fx)
            prev["n"] += 1
        else:
            merged.append(st)
    return [{**_step(st["ops"], st["cells"], names), **({"steps": st["n"]} if st["n"] > 1 else {}), "by": st["by"],
             "reason": st["reason"]} for st in merged]


def _writer(c: ToolCtx, cid: str) -> dict | None:
    """Who is writing changeset `cid` now (its write lease), as an agent reads it: the assistant or an external agent
    by its name (live.holder), and whether that is you. None: nobody."""
    le = c.srv.leases.get(cid)
    if le is None:
        return None
    return {"who": live.holder(le), "you": le.owner == c.owner}


def _me(c: ToolCtx) -> str:
    """Who the caller is, as the history and the review name it (live.holder's words): a call that finds another's
    changeset as its own (a helper script shared by two agents, a sessions file of another) sees whose session it is
    in."""
    if c.owner_kind != "mcp" or not c.owner:
        return "the assistant"
    sid = c.owner.removeprefix("mcp:").partition(":")[0]
    return f"{c.actor.removeprefix('agent:')} (MCP session {sid[:8]})"


def _not_you(c: ToolCtx) -> str:
    """What an agent that finds another's changeset as its own does (open_changeset's "already open"): the shell
    client's agents a --session of their own, the agents sharing another MCP connection a name (agent); the assistant
    has a session of its own, and is told nothing."""
    if c.owner_kind != "mcp":
        return ""
    if c.actor.removeprefix("agent:").split("/")[0] == "undine-cli":
        return ". If that is not you, your calls run in another agent's session: use a --session (or agent) of your own"
    return ". If that is not you, your calls run as another agent: give your own name (agent)"


def _lapses(c: ToolCtx, cid: str) -> str:
    """When the write lock another holds on `cid` ends: an MCP agent's lapses without writes (live.MCP_LEASE_S), the
    assistant's lasts its turn."""
    le = c.srv.leases.get(cid)
    if le is not None and le.kind == "turn":
        return "the assistant holds its lock until its turn ends or the person stops it"
    return f"its lock lapses after {live.MCP_LEASE_S:.0f} s without a write"


# ---------------------------------------------------------------------------------- changeset + writing tools


def open_changeset(c: ToolCtx, a: dict):
    if a.get("id"):                       # continue an open changeset (e.g. after reconnecting)
        try:
            meta = c.cs._load(c.project, a["id"])
        except Exception as e:
            raise ToolError(f"no changeset {a['id']} in {c.project}; list_changesets shows the open ones") from e
        if meta["status"] != "open":
            raise ToolError(f"changeset {a['id']} is {meta['status']}; open a new one")
        c.changeset = meta["id"]
        out = {"id": meta["id"], "title": meta["title"], "branch": (meta.get("git") or {}).get("branch"),
               "you": _me(c), "note": "continuing this changeset; get_changeset shows what is in it"}
        if (w := _writer(c, meta["id"])) and not w["you"]:
            out["writer"] = w
            out["note"] += (f"; {w['who']} is writing it now: your edits are refused until it stops "
                            f"({_lapses(c, meta['id'])}). Open a changeset of your own unless you are to take its "
                            "work over")
        return out
    held = None                           # your changeset, which another writer holds now: a new one of your own
    if c.changeset and c.cs._load(c.project, c.changeset)["status"] == "open":
        if not ((held := _writer(c, c.changeset)) and not held["you"]):
            meta = c.cs._load(c.project, c.changeset)
            return {"id": c.changeset, "title": meta["title"], "you": _me(c),
                    "note": f"already open in this session ({_me(c)}, opened by {meta.get('actor')}); keep editing it"
                            + _not_you(c)}
    title = (a.get("title") or "").strip()
    if not title:
        raise ToolError("title is required" + (f" (to open one of your own: {held['who']} is writing {c.changeset})"
                                               if held else ""))
    try:
        meta = c.cs.create(c.project, title, a.get("reason", ""), c.actor)
    except ChangesetError as e:          # a repository not set up for Undine: the person sets it up
        raise ToolError(str(e)) from e
    c.changeset = meta["id"]
    branch = (meta.get("git") or {}).get("branch")
    out = {"id": meta["id"], "branch": branch, "you": _me(c),
           "note": f"working copy created on the git branch {branch}; the real design is unchanged until a person "
                   "applies this changeset"}
    if meta.get("git_setup"):
        first, note = meta.get("first_commit"), meta.get("first_commit_note")
        out.update(git_setup=True, first_commit=first)
        out["note"] += ". The project was not in a git repository: Undine made it one (git init, set up for Undine)"
        if first:
            out["note"] += (" and committed the project's files as they were as its first commit (Undine's own output "
                            "and the changeset records left out), so that an applied changeset is committed on top")
        out["note"] += f" ({note}). Tell the user" if note else ". Tell the user"
    return out


def list_changesets(c: ToolCtx, a: dict):
    """The open changesets, each with who opened it, whether it is yours (the one this session, or this agent of it,
    writes) and who is writing it now; how many are closed."""
    rows = c.cs.list(c.project)
    out = []
    for r in rows:
        if r["status"] == "open":
            out.append({"id": r["id"], "title": r["title"], "by": r["actor"], "created": r["created"],
                        **({"yours": True} if r["id"] == c.changeset else {}),
                        **({"writer": w} if (w := _writer(c, r["id"])) else {})})
    return {"open": out, "closed": len([r for r in rows if r["status"] != "open"]), "you": _me(c),
            "note": "continue one with open_changeset(id); nobody but its writer edits it while it writes (an agent's "
                    f"lock lapses after {live.MCP_LEASE_S:.0f} s idle, the assistant's lasts its turn): one that another "
                    "agent is writing (writer) is not yours. "
                    "get_changeset(id) reads any of them, also a closed one"}


def create_cell(c: ToolCtx, a: dict):
    """A new cell in a library of the working copy: an empty schematic, a symbol (pins from the schematic's ports) and /
    or an empty layout cell. Recorded as operations, so applying the changeset adds the cell to the real project."""
    with c.srv.changeset_lock:
        return _create_cell(c, a)


def _create_cell(c: ToolCtx, a: dict):
    from undine.store.project import check_name

    work = c.need_changeset()
    lib, views = a.get("lib"), a.get("views") or ["schematic"]
    _check_limit(c)
    if not lib or not a.get("cell"):
        raise ToolError("lib and cell are required")
    if not (a.get("reason") or "").strip():
        raise ToolError("reason is required: say why this change is made")
    if not isinstance(views, list) or not views or set(views) - {"schematic", "symbol", "rtl", "layout"}:
        raise ToolError("views must be a list of schematic, symbol, rtl and / or layout")
    try:
        library = c.srv.project(work).library(lib)
        if library.readonly:
            raise ToolError(f"library {lib} is read-only (a dependency or the PDK); create the cell in a project library")
        name = check_name(a["cell"])
    except (KeyError, ValueError) as e:
        raise ToolError(str(e).strip("'\"")) from e
    views = [v for v in ("schematic", "symbol", "rtl", "layout") if v in views]
    exists = {"schematic": (library.path / f"{name}.sch").exists(), "symbol": (library.path / f"{name}.sym").exists(),
              "rtl": rtl.find_file(library.path, name) is not None,
              "layout": name in ((_layout_cells(c, work, lib, library) or ([], []))[0])}
    if taken := [v for v in views if exists[v]]:
        raise ToolError(f"{lib}/{name} already has a {' and a '.join(taken)}; edit it, or create only the missing views"
                        + ("; sync_symbol updates or redraws a symbol from the schematic's ports" if "symbol" in taken else ""))
    if "symbol" in views and not exists["schematic"]:
        raise ToolError("a symbol takes its pins from the schematic's ports: create the schematic first, add its ports "
                        "(ipin / opin / iopin symbols), then create the symbol")
    why = a["reason"]
    prj = c.srv.project(work)
    for v in views:
        if v == "layout":
            db = c.srv.db(work, lib)
            with db.recording("create_cell", {"cell": name, "view": v}, c.actor, why, c.changeset):
                db.new_cell(name)
        elif v == "rtl":
            ed = c.srv.rtl_editor(work, lib, name)
            with ed.recording("create_cell", {"cell": name, "view": v}, c.actor, why, c.changeset):
                ed.create_file(a.get("rtl"))
        else:
            ed = c.srv.editor(work, lib, name, v)
            if v == "schematic":
                text = xschem.dump(xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n"))
            else:
                sch = library.path / f"{name}.sch"
                ports = schematic.connectivity(sch, xschem.SymbolResolver(prj.symbol_paths())).ports if sch.is_file() else []
                text = xschem.dump(xschem.make_symbol(ports, _intent_sides(c, lib, name)))
            with ed.recording("create_cell", {"cell": name, "view": v}, c.actor, why, c.changeset):
                ed.create_file(text)
    c.writes += 1
    out = {"ok": True, "cell": f"{lib}/{name}", "created": views}
    if "symbol" in views and not (prj.library(lib).path / f"{name}.sch").is_file():
        out["note"] = "the cell has no schematic, so the symbol has no pins"
    return out


def set_instance_params(c: ToolCtx, a: dict):
    inst, params = a.get("instance"), a.get("params")
    if not inst or not isinstance(params, dict) or not params:
        raise ToolError("instance and a non-empty params object are required, e.g. {\"W\": \"1.5\"}")
    clean = {str(k): (None if v is None else str(v)) for k, v in params.items()}
    new_id = _write(c, a, "set_props", lambda ed: ed.set_props("i", inst, clean))
    out = {"ok": True, "instance": new_id, "set": clean}
    if (applies := _intent_on(c, a, [inst, new_id])):
        out["intent"] = applies
        out["note"] = "this instance is named in the cell's design intent: check that the change keeps it"
    return out


def add_instance(c: ToolCtx, a: dict):
    sym = a.get("symbol")
    if not sym:
        raise ToolError("symbol is required, e.g. sky130_fd_pr/nfet_01v8.sym")
    x, y = (float(a["x"]), float(a["y"])) if a.get("x") is not None and a.get("y") is not None else (None, None)
    name = _write(c, a, "add_instance", lambda ed: ed.add_instance(sym, x, y, int(a.get("rot", 0)), int(bool(a.get("flip"))),
                                                                   a.get("params") or {}, placed="auto"))
    return {"ok": True, "instance": name, **({"note": "placed in free space next to the drawing"} if x is None else {})}


def add_wire(c: ToolCtx, a: dict):
    try:
        x1, y1, x2, y2 = (float(a[k]) for k in ("x1", "y1", "x2", "y2"))
    except (KeyError, TypeError, ValueError) as e:
        raise ToolError("x1, y1, x2, y2 are required numbers (schematic units)") from e
    wid = _write(c, a, "add_wire", lambda ed: ed.add_wire(x1, y1, x2, y2, a.get("label", "")))
    return {"ok": True, "wire": wid}


LABEL_SYMBOL = "devices/lab_pin.sym"


STYLES = ("auto", "wire", "label")


def _connect(c: ToolCtx, a: dict, op: str, nets: list[tuple[str, list]]) -> list[dict]:
    """Draws each net (routed wires, labels); a net the drawing cannot take (no route, or it would join other nets)
    goes into the netlist as pending connections instead, shown as a ratsnest for a person to draw."""
    style = a.get("style") or "auto"
    if style not in STYLES:
        raise ToolError("style is auto (wires, labels for supplies and large nets), wire or label")
    if not (a.get("reason") or "").strip():
        raise ToolError("reason is required: say why this change is made")
    move = bool(a.get("move"))                   # a pin drawn to another net is taken off it first
    try:
        return _write(c, a, op, lambda ed: ed.connect_nets(nets, style, LABEL_SYMBOL, move))
    except ToolError:
        pass                                     # all or nothing failed: draw net by net
    out = []
    for net, pins in nets:                       # one by one: draw what can be drawn, keep the rest pending
        try:
            out += _write(c, a, op, lambda ed, net=net, pins=pins: ed.connect_nets([(net, pins)], style, LABEL_SYMBOL, move))
        except ToolError as e:
            lib, cell = _lib_cell(a)
            nl = c.srv.netlist_editor(c.work, lib, cell)
            try:
                with nl.recording(op, {"net": net, "pins": pins}, c.actor, a.get("reason"), c.changeset):
                    nl.connect_many([(*str(p).split(":", 1), net) for p in pins])
            except (KeyError, ValueError) as e2:
                kept = [r["net"] for r in out]
                raise ToolError(f"{net}: not drawn ({e}) and not added as pending ({e2})"
                                + (f". The nets before it are kept: {', '.join(kept)}" if kept else "")
                                + (". A pin drawn to another net moves with move: true" if "drawn to" in str(e2) else "")) from e2
            out.append({"net": net, "wires_added": 0, "labelled": [], "pending": pins, "why_not_drawn": str(e)})
    return out


def connect_pins(c: ToolCtx, a: dict):
    """Puts the device pins `pins` ("M1:d") on net `net`: routed wires, or net labels (no coordinates needed)."""
    r = _connect(c, a, "connect_pins", [(a.get("net"), a.get("pins"))])[0]
    return {"ok": True, **r,
            "note": "wires carry the net name; labelled pins join it by name, as does a port (ipin / opin / iopin) whose lab is the net"}


def connect_nets(c: ToolCtx, a: dict):
    """connect_pins for many nets in one operation: {net: [pins]}."""
    nets = a.get("nets")
    if not isinstance(nets, dict) or not nets:
        raise ToolError('nets is required: {"out": ["M2:D", "M4:D"], "vdd": ["M3:S", "M4:S"]}')
    rs = _connect(c, a, "connect_nets", list(nets.items()))
    pend = {r["net"]: r["pending"] for r in rs if r.get("pending")}
    moved = {p: was for r in rs for p, was in (r.get("moved_from") or {}).items()}
    return {"ok": True, "nets": len(rs), "wires_added": sum(r["wires_added"] for r in rs),
            "labelled": {r["net"]: r["labelled"] for r in rs if r["labelled"]},
            **({"moved_from": moved} if moved else {}),
            **({"pending": pend, "note": "pending nets are in the netlist but not drawn (a ratsnest in the schematic)"} if pend else {})}


def add_instances(c: ToolCtx, a: dict):
    """add_instance for many symbols in one operation."""
    items = a.get("instances")
    if not isinstance(items, list) or not items or not all(isinstance(i, dict) and i.get("symbol") for i in items):
        raise ToolError('instances is required: [{"symbol": "sky130_fd_pr/nfet_01v8.sym", "x": 0, "y": 0, "params": {...}}, ...]')

    def fn(ed):
        return [ed.add_instance(i["symbol"], float(i["x"]) if i.get("x") is not None else None,
                                float(i["y"]) if i.get("y") is not None else None, int(i.get("rot", 0)),
                                int(bool(i.get("flip"))), i.get("params") or {}, placed="auto") for i in items]
    return {"ok": True, "instances": _write(c, a, "add_instances", fn)}


def get_netlist(c: ToolCtx, a: dict):
    """The netlist view: ports, instances (all, or those named / on the nets named, a page at a time), the code blocks'
    text (a testbench's .meas / .control / .options) and pending connections."""
    lib, cell = _lib_cell(a)
    try:
        v = c.srv.netlist_view(c.work, lib, cell)
        ed = c.srv.netlist_editor(c.work, lib, cell)
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    m = v["netlist"]
    sup = v.get("supplies") or {}
    # warnings first (a supply parameter naming no net of the schematic ...); a supply parameter once (supply_pins)
    names, code = list(m["instances"]), m["code"]
    out = {**({"warnings": v["warnings"]} if v.get("warnings") else {}), "ports": m["ports"]}
    if (want := a.get("instances")):
        code = [x for x in code if x.get("name") in want]
        if (unknown := [n for n in want if n not in m["instances"] and n not in {x.get("name") for x in code}]):
            out["not_found"] = unknown
        names = [n for n in names if n in set(want)]
    if (nets := a.get("nets")):                  # pins, and supply parameters (a standard cell's VPWR=vdd) on them
        on = set(nets)                           # and the code blocks naming them (Vdd vdd 0 1.8), as SPICE: any case
        code = [x for x in code if {n.lower() for n in on} & netlist.code_nodes({"code": [x]})]
        names = [n for n in names if on & {*m["instances"][n]["pins"].values(), *sup.get(n, {}).values()}]
        out["nets"] = {net: [f"{n}:{p}" for n, r in m["instances"].items() for p, x in r["pins"].items() if x == net]
                       + [f"{n}:{p}" for n, s in sup.items() for p, x in s.items() if x == net] for net in nets}
    try:
        off, lim = max(int(a.get("offset") or 0), 0), int(a["limit"]) if a.get("limit") else None
    except (TypeError, ValueError) as e:
        raise ToolError("offset and limit are whole numbers (instances to skip, and to list)") from e
    page = names[off:][:lim]
    defaults: dict[str, dict] = {}

    def params(n: str, r: dict) -> dict:
        """The instance's parameters, those left at their symbol's formula defaults (sky130's ad / as / pd / ps ...) out,
        and its supply parameters (in supply_pins)."""
        props = {k: x for k, x in r.get("props", {}).items() if k not in sup.get(n, {})}
        if a.get("all_params"):
            return props
        if r["symbol"] not in defaults:
            defaults[r["symbol"]] = schematic.sym_template(ed.resolver.load(r["symbol"], ed.sch.parent))
        d = defaults[r["symbol"]]
        return {k: x for k, x in props.items() if not (x == d.get(k) and _formula(str(x)))}
    out["instances"] = {n: {"symbol": m["instances"][n]["symbol"], "pins": m["instances"][n]["pins"],
                            **({"supply_pins": sup[n]} if n in sup else {}),
                            "params": params(n, m["instances"][n])} for n in page}
    if want or nets or off or lim:
        out["shown"] = {"instances": len(page), "of": len(names), "offset": off,
                        **({"next_offset": off + len(page)} if off + len(page) < len(names) else {})}
    try:
        climit = int(a["code_limit"]) if a.get("code_limit") is not None else None
    except (TypeError, ValueError) as e:
        raise ToolError("code_limit is a whole number of characters per code block") from e
    if code and not off and a.get("code", True) is not False:
        out["code"] = [{"name": x.get("name", ""), "symbol": x["symbol"],
                        **{k: _clip(v, climit, x.get("name", "")) for k, v in x.get("props", {}).items()}} for x in code]
    elif code and a.get("code") is False:
        out["code_blocks"] = [x.get("name", "") for x in code]
    keep = set(page)
    out["pending"] = {k: x for k, x in m["pending"].items() if k.split(".")[0] in keep} if want or nets or off or lim else m["pending"]
    out["note"] = ("pending connections are in the netlist and not drawn yet (a ratsnest in the schematic); the netlisters "
                   "use them. Parameters left at their symbol's formula defaults are not listed (all_params lists them). "
                   "supply_pins: pins a symbol takes as parameters (standard cells' VPWR / VGND / VPB / VNB), connected by "
                   "net name. Wires, labels and positions: get_drawing"
                   + ("; code: the code blocks' text (value), in order" if out.get("code") else "")
                   + ("; code_blocks: the code blocks left out (code: false), by name" if out.get("code_blocks") else ""))
    return out


def _clip(v, limit: int | None, name: str):
    """A code block's text cut to `limit` characters, saying how much is left out and how to read it (get_netlist
    code_limit)."""
    if limit is None or not isinstance(v, str) or len(v) <= max(limit, 0):
        return v
    return v[:max(limit, 0)] + f"… ({len(v) - max(limit, 0)} more characters; all of it: instances ['{name}'] without code_limit)"


def _formula(v: str) -> bool:
    return "@" in v or v.startswith(("'", '"', "expr(")) or "int(" in v


def tidy_schematic(c: ToolCtx, a: dict):
    moved = _write(c, a, "tidy", lambda ed: ed.tidy())
    return {"ok": True, "moved": moved, "note": "only symbols placed by tools with nothing drawn to them move; what a person placed stays"}


WIRE_ID = re.compile(r"[-+.\de]+(,[-+.\de]+){3}")     # a wire's id, its end points: "x1,y1,x2,y2" (get_drawing)


def delete_objects(c: ToolCtx, a: dict):
    objs = a.get("objects") or []
    items = [{"k": {"instance": "i", "wire": "w"}.get(o.get("kind")), "id": o.get("id")} if isinstance(o, dict) else
             {"k": "w" if WIRE_ID.fullmatch(str(o)) else "i", "id": o} for o in objs]
    if not items or any(not i["k"] or not i["id"] for i in items):
        raise ToolError("objects: [{kind: 'instance'|'wire', id}] is required (or instance names and wire ids, e.g. "
                        "['M1', 'M2', '0,-20,0,40'])")
    _write(c, a, "delete", lambda ed: ed.delete(items))
    return {"ok": True, "deleted": len(items)}


# ---------------------------------------------------------------------------------- schematic drawing and symbols
# Schematic units: 10 per grid step. A wire's id is its end points, "x1,y1,x2,y2" with the ends sorted.

SCH_GRID = 10


def _number(v) -> float:
    try:
        out = float(v)
    except (TypeError, ValueError) as e:
        raise ToolError(f"{v!r} is not a number") from e
    if out != out or abs(out) == float("inf"):
        raise ToolError(f"{v!r} is not a finite number")
    return out


OVERLAPS_LISTED = 60     # overlapping pairs a shortened get_drawing lists


def _fits(v, limit: int | None = RESULT_CHARS) -> bool:
    """Whether v fits in a result of `limit` characters (the call's budget, _budget; None: any size does)."""
    return not limit or len(results.compact(v)) <= limit


def get_drawing(c: ToolCtx, a: dict):
    """How the schematic is drawn (SchEditor.drawing): positions and extents, each net's wires and labels, overlaps.
    Overlaps and the note come first; a drawing too large for an answer is told in less detail, step by step."""
    lib, cell = _lib_cell(a)
    region = a.get("region")
    if region is not None and (not isinstance(region, list) or len(region) != 4):
        raise ToolError("region is [x1, y1, x2, y2] in schematic units")
    try:
        d = c.srv.editor(c.work, lib, cell, "schematic").drawing(a.get("net") or None, a.get("instances") or None,
                                                                  [_number(v) for v in region] if region else None)
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    note = ("units: 10 per grid step, y grows downwards. extent is the instance's drawing with its texts; overlaps lists "
            "instances whose extents overlap (move one with move_instance, view schematic). Wire ids go to delete_objects; "
            "label_net replaces wires by labels")
    out = {"overlaps": d["overlaps"], "note": note, "instances": d["instances"], "nets": d["nets"]}
    if _fits(out, limit := _budget(c)):
        return out
    ask = "ask with net (its wire ids, labels, pins), instances or region [x1, y1, x2, y2] for the details"
    over = {"overlaps": d["overlaps"][:OVERLAPS_LISTED],
            **({"overlaps_total": len(d["overlaps"])} if len(d["overlaps"]) > OVERLAPS_LISTED else {})}
    for shown, what in (({"instances": {k: {"at": v["at"], "extent": v["extent"]} for k, v in d["instances"].items()},
                          "nets": {n: {"wires": len(r["wires"]), "labels": len(r["labels"]), "pins": len(r["pins"])}
                                   for n, r in d["nets"].items()}}, "positions and extents, counts per net"),
                        ({"instances": list(d["instances"]), "nets": list(d["nets"])}, "the names only"),
                        ({"instances": len(d["instances"]), "nets": len(d["nets"])}, "counts only")):
        out = {**over, "note": f"large drawing: {what}; {ask}. {note}", **shown}
        if _fits(out, limit):
            break
    return out


def _move_schematic(c: ToolCtx, a: dict):
    names = a.get("instances") or ([a["instance"]] if a.get("instance") else [])
    if not names or not all(isinstance(n, str) and n for n in names):
        raise ToolError("instance (or instances: [names]) is required")
    lib, cell = _lib_cell(a)
    if a.get("x") is not None or a.get("y") is not None:      # a target for the (first) instance: the rest move with it
        try:
            f = c.srv.editor(c.work, lib, cell, "schematic")._load()
        except KeyError as e:
            raise ToolError(str(e).strip("'\"")) from e
        comp = dict(zip(xschem.comp_ids(f), f.comps)).get(names[0])
        if comp is None:
            raise ToolError(f"no instance {names[0]} in {lib}/{cell}")
        to = (_number(a["x"]) if a.get("x") is not None else comp.x, _number(a["y"]) if a.get("y") is not None else comp.y)
        dx, dy = round(to[0] / SCH_GRID) * SCH_GRID - comp.x, round(to[1] / SCH_GRID) * SCH_GRID - comp.y
    else:
        dx, dy = (round(_number(a.get(k) or 0) / SCH_GRID) * SCH_GRID for k in ("dx", "dy"))
    if not dx and not dy:
        return {"ok": True, "moved": [], "note": "already there"}
    items = [{"k": "i", "id": n} for n in names]
    try:
        res = _write(c, a, "move", lambda ed: ed.move(items, dx, dy, owned=True))
    except ToolError as e:
        if "route" not in str(e) and "connections" not in str(e):
            raise
        raise ToolError(f"{e}. The wires attached are re-routed with the move; when that is not possible nothing moves: "
                        "try another place, or move the instances it is wired to together (instances)") from e
    return {"ok": True, "moved": names, "by": [dx, dy], "result": res,
            "note": "wires attached to the moved pins were re-routed; tidy_schematic leaves these instances where they are now"}


def disconnect_pins(c: ToolCtx, a: dict):
    """Takes device pins off their nets (SchEditor.disconnect), drawn or pending."""
    pins = a.get("pins")
    if not isinstance(pins, list) or not pins or not all(isinstance(p, str) and ":" in p for p in pins):
        raise ToolError('pins is required: ["M1:D", "x3:B"] (instance:pin)')
    lib, cell = _lib_cell(a)
    c.need_changeset()
    if not (a.get("reason") or "").strip():
        raise ToolError("reason is required: say why this change is made")
    try:
        pending = c.srv.netlist_view(c.work, lib, cell)["netlist"]["pending"]
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    drawn = [p for p in pins if p.replace(":", ".", 1) not in pending]
    out: dict = {"ok": True}
    if drawn:
        out["disconnected"] = _write(c, a, "disconnect_pins", lambda ed: ed.disconnect(drawn, LABEL_SYMBOL))
    if undrawn := [p for p in pins if p not in drawn]:
        nl = c.srv.netlist_editor(c.work, lib, cell)
        with c.srv.changeset_lock, nl.recording("disconnect_pins", {"pins": undrawn}, c.actor, a["reason"], c.changeset):
            for p in undrawn:
                nl.disconnect(*p.split(":", 1))
        out["pending_removed"] = undrawn
    out["note"] = ("the pins are free now; the rest of each net stays connected (re-joined where taking the pin off cut it "
                   "apart). Connect them with connect_pins / connect_nets")
    return out


def label_net(c: ToolCtx, a: dict):
    if not a.get("net"):
        raise ToolError("net is required (its name in get_netlist)")
    wires = a.get("wires")
    if wires is not None and (not isinstance(wires, list) or not all(isinstance(w, str) for w in wires)):
        raise ToolError("wires is a list of wire ids ('x1,y1,x2,y2', from get_drawing)")
    res = _write(c, a, "label_net", lambda ed: ed.label_net(a["net"], wires or None, LABEL_SYMBOL))
    return {"ok": True, **res, "note": "the circuit is unchanged: each piece of the net left by the wires taken out "
                                       "carries the net's name on a label"}


def set_port_order(c: ToolCtx, a: dict):
    order = a.get("order")
    if not isinstance(order, list) or not order:
        raise ToolError("order is required: every port name once, in the subcircuit's pin order")
    res = {"ok": True, "order": _write(c, a, "set_port_order", lambda ed: ed.set_port_order([str(p) for p in order]))}
    lib, cell = _lib_cell(a)
    if a.get("symbol", True) and (c.srv.project(c.work).library(lib).path / f"{cell}.sym").is_file():
        try:                                     # the pins' order only: nothing a schematic placing it has to follow
            res["symbol"] = _sync_symbol(c, a, {}, False, "set_port_order", order_only=True)
        except (ToolError, live.WriteLocked) as e:
            res["symbol"] = {"error": str(e), "note": "the schematic's port order is set; the symbol was not changed "
                                                      "(set_port_order again, or sync_symbol)"}
        if res["symbol"].get("not_in_symbol") or res["symbol"].get("not_ports"):
            res["symbol"]["note"] = "the symbol's pins differ from the ports otherwise (check_symbol); sync_symbol makes them follow"
    return res


def _intent_sides(c: ToolCtx, lib: str, cell: str) -> dict[str, str]:
    """The port sides the cell's intent gives (N, S, E, W; auto leaves the choice to the tool)."""
    try:
        ports = c.srv.intent_editor(c.work, lib, cell).model().get("ports") or {}
    except Exception:
        return {}
    return {n: p["side"] for n, p in ports.items() if isinstance(p, dict) and p.get("side") in xschem.SIDES}


def _sync_symbol(c: ToolCtx, a: dict, sides: dict, regenerate: bool, op: str, order_only: bool = False) -> dict:
    from undine.schematics import symbolsync
    lib, cell = _lib_cell(a)
    with c.srv.changeset_lock:
        c.need_changeset()
        _check_limit(c)
        if not (a.get("reason") or "").strip():
            raise ToolError("reason is required: say why this change is made")
        try:
            res = symbolsync.from_schematic(c.srv, c.work, lib, cell, sides, regenerate, c.actor, a["reason"], c.changeset, op,
                                            order_only)
        except (KeyError, ValueError, PermissionError) as e:
            raise ToolError(str(e).strip("'\"")) from e
        c.writes += 1
        return {k: v for k, v in res.items() if v not in ([], {}, None, False) or k in ("changed", "created")}


def sync_symbol(c: ToolCtx, a: dict):
    lib, cell = _lib_cell(a)
    sides = {**_intent_sides(c, lib, cell), **{k: str(v).upper() for k, v in (a.get("sides") or {}).items()}}
    if bad := [f"{k}: {v}" for k, v in sides.items() if v not in xschem.SIDES]:
        raise ToolError(f"sides are N, S, E or W ({', '.join(bad)})")
    res = _sync_symbol(c, a, sides, bool(a.get("regenerate")), "sync_symbol")
    res["note"] = ("the symbol's pins follow the schematic's ports (order, directions, names) and the sides asked for "
                   "(from the cell intent's port sides unless given); schematics that place it keep their connections: "
                   "wiring of a moved pin follows it (affected)")
    return res


def _schematic_base(c: ToolCtx, lib: str, cell: str) -> list[str] | None:
    """_schematic_findings of the cell in the changeset's baseline (None: it has no schematic there, or none is
    known)."""
    try:
        base = c.cs._load(c.project, c.changeset)["baseline"]
        if not (c.srv.project(base).library(lib).path / f"{cell}.sch").is_file():
            return None
        return _schematic_findings(c, lib, cell, base)
    except (KeyError, ValueError, OSError, ChangesetError):
        return None


def _symbol_issues(c: ToolCtx, lib: str, cell: str, work: str | None = None) -> dict | None:
    """symbolcheck's comparison of the cell's schematic ports and symbol pins (None without a schematic or symbol), in
    the working copy (or project `work`)."""
    from undine.schematics import symbolcheck
    prj = c.srv.project(work or c.work)
    d = prj.library(lib).path
    if not (d / f"{cell}.sch").is_file() or not (d / f"{cell}.sym").is_file():
        return None
    return symbolcheck.compare(symbolcheck.capture(prj, lib, cell))


def check_symbol(c: ToolCtx, a: dict):
    from undine.schematics import symedit
    lib, cell = _lib_cell(a)
    prj = c.srv.project(c.work)
    d = prj.library(lib).path
    if not (d / f"{cell}.sch").is_file():
        raise ToolError(f"{lib}/{cell} has no schematic: a symbol is checked against the schematic's ports")
    if not (d / f"{cell}.sym").is_file():
        return {"match": False, "issues": [{"code": "missing_symbol", "message": "The cell has no symbol view."}],
                "fix": "sync_symbol creates it from the ports (sides from the intent)"}
    cmp = _symbol_issues(c, lib, cell)
    sym = xschem.parse((d / f"{cell}.sym").read_text(errors="replace"))
    have = symedit.pin_sides(sym)
    wanted = _intent_sides(c, lib, cell)
    sides = {n: {"symbol": have.get(n), "intent": wanted[n]} for n in wanted if have.get(n) and have[n] != wanted[n]}
    _, plan = symedit.from_ports(sym, [(p["name"], {"in": "ipin", "out": "opin"}.get(p["direction"], "iopin"))
                                       for p in cmp["schematic_ports"]], wanted)
    out = {"match": cmp["match"] and not sides,
           "issues": [{k: v for k, v in i.items() if k != "severity"} for i in cmp["issues"]],
           "schematic_ports": [p["name"] for p in cmp["schematic_ports"]], "symbol_pins": [p["name"] for p in cmp["symbol_pins"]],
           "pin_sides": have}
    if sides:
        out["sides_differ_from_intent"] = sides
    if not out["match"]:
        fix = {k: plan[k] for k in ("added", "removed", "moved", "directions") if plan.get(k)}
        if plan.get("order_changed"):
            fix["order"] = plan["order"]
        out["fix"] = {"sync_symbol would": fix or "redraw it", "regenerated": bool(plan.get("regenerated"))}
    return out


def _schematic_findings(c: ToolCtx, lib: str, cell: str, work: str | None = None) -> list[str]:
    """What is wrong with a cell's schematic short of a simulation: the symbol against the ports, supply parameters
    naming no net, connections not drawn, symbols that do not resolve (in the working copy, or project `work`)."""
    from undine.schematics import netlist as nl
    work = work or c.work
    out = []
    cmp = _symbol_issues(c, lib, cell, work)
    if cmp and not cmp["match"]:
        out += [f"symbol: {i['message']}" + (f" (schematic: {i['expected']}; symbol: {i['actual']})" if i.get("expected") else "")
                for i in cmp["issues"][:6]]
        out.append("symbol: sync_symbol makes it follow the schematic")
    try:
        v = c.srv.netlist_view(work, lib, cell)
    except KeyError:
        return out
    out += v.get("warnings") or []
    if v["netlist"]["pending"]:
        out.append(f"{len(v['netlist']['pending'])} connections in the netlist are not drawn (pending)")
    sch = c.srv.project(work).library(lib).path / f"{cell}.sch"
    out += nl.drawing_warnings(sch, xschem.SymbolResolver(c.srv.project(work).symbol_paths()))
    return out


# ---------------------------------------------------------------------------------- layout tools
# Coordinates are in micrometres (the layout grid is 5 nm; values are snapped to it).

GRID_UM = 0.005


def _um(v) -> float:
    try:
        return round(round(float(v) / GRID_UM) * GRID_UM, 6)      # 17.865, not 17.865000000000002
    except (TypeError, ValueError) as e:
        raise ToolError(f"{v!r} is not a number") from e


def _ldb(c: ToolCtx, a: dict, write: bool = True, need: bool = True):
    """(working copy, lib, cell, its LayoutDB). need: the cell must have a layout, else the error says how it gets one
    (sync_layout from its schematic)."""
    lib, cell = _lib_cell(a)
    work = c.need_changeset() if write else c.work
    try:
        db = c.srv.db(work, lib)
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    if need and db.layout.cell(cell) is None:
        library = c.srv.project(work).library(lib)
        if (library.path / f"{cell}.sch").is_file():
            raise ToolError(f"{cell} has no layout view yet: sync_layout creates one from its schematic (or create_cell "
                            "with views [layout], empty)")
        if any((library.path / f"{cell}.{x}").is_file() for x in ("sym", "sv", "v")):
            raise ToolError(f"{cell} has no layout view: create_cell with views [layout] makes an empty one")
        raise ToolError(f"no cell {cell} in {lib}: list_cells shows its cells")
    return work, lib, cell, db


def _lwrite(c: ToolCtx, a: dict, op: str, fn, need: bool = True):
    work, lib, cell, db = _ldb(c, a, need=need)
    _check_limit(c)
    if not (a.get("reason") or "").strip():
        raise ToolError("reason is required: say why this change is made")
    args = {k: v for k, v in a.items() if k not in ("reason", "lib", "cell")}
    try:
        with db.recording(op, {"cell": cell, **args}, c.actor, a["reason"], c.changeset):
            res = fn(db, cell)
    except (KeyError, ValueError, PermissionError) as e:
        raise ToolError(str(e).strip("'\"")) from e
    c.writes += 1
    return res


def _moved(snapped: list[float], given: list) -> bool:
    """Whether the grid moved a coordinate (by more than a float's rounding: 17.8651, not 33.800000000000004)."""
    return any(abs(s - float(g)) > 1e-6 for s, g in zip(snapped, given))


def _box_um(a: dict, key: str = "box") -> list[float]:
    b = a.get(key)
    if not isinstance(b, list) or len(b) != 4:
        raise ToolError(f"{key} must be [x1, y1, x2, y2] in micrometres")
    return [_um(v) for v in b]


def _array_um(arr: dict, k: float) -> dict:
    """An array instance's size and steps in um: pitch_um when it steps along x and y (as place_cell's), else its vectors."""
    out = {"columns": arr["columns"], "rows": arr["rows"]}
    if arr["a"][1] == 0 and arr["b"][0] == 0:
        return {**out, "pitch_um": [round(arr["a"][0] * k, 3), round(arr["b"][1] * k, 3)]}
    return {**out, "a_um": [round(v * k, 3) for v in arr["a"]], "b_um": [round(v * k, 3) for v in arr["b"]]}


UNBOUND_LISTED = 40      # unbound instances listed one by one (with their ids); beyond, counted by cell


def _inst_um(i: dict, k: float) -> dict:
    """A layout instance as the tools show it: id, cell, bbox (um), what it is bound to, its array and layout
    parameters, the tool a designer placed a bound piece with (placed_with: sync_layout leaves it)."""
    return {**({"name": i["name"]} if i.get("name") else {}), "id": i["id"], "cell": i["cell"],
            "bbox_um": [round(v * k, 3) for v in i["bbox"]], **({"array": _array_um(i["array"], k)} if "array" in i else {}),
            **({"layout": i["layout"]} if i.get("layout") else {}), **({"placed_with": i["own"]} if i.get("own") else {})}


def get_layout_info(c: ToolCtx, a: dict):
    work, lib, cell, db = _ldb(c, a, write=False)
    k = db.layout.dbu
    info = db.info(cell)
    by_layer: dict[str, int] = {}
    cc = db.cell(cell)
    for li in db.layout.layer_indexes():
        n = cc.shapes(li).size()
        if n:
            by_layer[db._layer_key(li)] = n
    insts = db.instances(cell)
    unbound = [i for i in insts if not i.get("name")]
    out = {"cell": cell, "dbu_um": k, "bbox_um": [round(v * k, 3) for v in info["bbox"]], "own_shapes_by_layer": by_layer,
           "instances": [_inst_um(i, k) for i in insts if i.get("name")], "unbound_instances": len(unbound)}
    if unbound:                      # standard cells, dummies, arrays ... placed without a schematic name: by id
        out["unbound"] = [_inst_um(i, k) for i in unbound[:UNBOUND_LISTED]]
        by_cell: dict[str, int] = {}
        for i in unbound:
            by_cell[i["cell"]] = by_cell.get(i["cell"], 0) + 1
        out["unbound_by_cell"] = dict(sorted(by_cell.items(), key=lambda kv: -kv[1])[:40])
    prj = c.srv.project(work)
    sch = prj.library(lib).path / f"{cell}.sch"
    if sch.is_file():
        try:
            from undine.layouts import connectivity, sdl as sdlmod
            sd = sdlmod.Sdl(prj.pdk, db, sch, xschem.SymbolResolver(prj.symbol_paths()))
            st = sd.status()
            out["schematic_sync"] = {"missing": st["missing"], "out_of_date": st["stale"], "extra": st["extra"],
                                     "unsupported": st["unsupported"]}
            if st["notes"]:                  # what the layout does not make of the schematic (MF of an unsupported device ...)
                out["schematic_sync"]["notes"] = st["notes"]
            if st["own_layout"]:             # bound arrays / cells of the designer's: sync_layout leaves them alone
                out["schematic_sync"]["own_layout"] = [{"name": d["name"], "layout": d["own_layout"], "schematic": d["expected_cell"]}
                                                       for d in st["devices"] if d.get("own_layout")]
            if (taps := sdlmod.taps([d for d in st["devices"] if d.get("placed")])):
                out["schematic_sync"]["taps"] = taps
            conn, devs = sd._devices(sd._stored())
            wiring = connectivity.check(prj.pdk, db.layout, cell, conn, devs, sd._bound()) if st["devices"] else None
            if wiring is not None:
                out["schematic_sync"]["unrouted_nets"] = [u["net"] for u in wiring["unrouted"]]
                out["schematic_sync"]["wiring"] = wiring
            else:                            # no conductor recipe for this PDK: only which nets join placed devices
                out["schematic_sync"]["nets_between_devices"] = sorted({r["net"] for r in st["ratsnest"]})
            out["hint"] = ("devices listed in out_of_date have different parameters in the schematic than in the layout: call sync_layout"
                           if st["stale"] else "layout devices match the schematic parameters")
        except Exception as e:
            out["schematic_sync"] = f"unavailable: {e}"
    return out


def _profile(c: ToolCtx, work: str):
    prj = c.srv.project(work)
    return prj.pdk.profile if prj.pdk else None


def _layer_names(db, prof) -> dict[tuple[int, int], str]:
    """(layer, datatype) -> the name an answer gives it: the profile's (met1, capm), else the layer table's (the PDK's
    layer properties: areaid.standardc), else nothing."""
    out: dict[tuple[int, int], str] = {}
    for name, _src, l, d in db._layer_table():
        out.setdefault((l, d), name.split(" - ")[0].removesuffix(".drawing"))
    if prof is not None:
        out.update({v: n for n, v in reversed(list(prof.layers.items()))})
    return out


def _layer_key(db, spec: str, cell: str, prof=None) -> tuple[int, int]:
    """(layer, datatype) of a layer as add_box takes it (a name of the layer table, 'layer/datatype'), or by the PDK
    profile's name for it (sky130: areaid.sc for the table's areaid.standardc)."""
    if prof is not None and str(spec) in prof.layers:
        return prof.layers[str(spec)]
    try:
        l, d = db.layer_source(db.layer_id(str(spec), cell)).split("/")
        return int(l), int(d)
    except (KeyError, ValueError) as e:
        raise ToolError(f"unknown layer {spec!r}: give a layer name (met1, capm, hvi, areaid.sc ...) or 'layer/datatype' "
                        "(68/20), as add_box takes it") from e


def _lid(c: ToolCtx, work: str, db, spec, cell: str) -> int:
    """The layer view id of a layer as the layout tools take it: a name of the layer table, 'layer/datatype', or the
    PDK profile's name for it, which the drawing tools answer in (IHP: diff and poly for Activ and GatPoly)."""
    prof = _profile(c, work)
    s = str(spec or "")
    if prof is not None and s in prof.layers:
        s = "%d/%d" % prof.layers[s]
    return db.layer_id(s, cell)


def _schematic_pins(c: ToolCtx, work: str, lib: str, cell: str) -> dict[str, dict[str, str]]:
    """{instance: {pin: net}} of the cell's schematic (its netlist view), {} when it has none."""
    prj = c.srv.project(work)
    sch = prj.library(lib).path / f"{cell}.sch"
    if not sch.is_file():
        return {}
    try:
        conn = schematic.connectivity(sch, xschem.SymbolResolver(prj.symbol_paths()), cell)
    except Exception:
        return {}
    return {d.name: dict(d.nets) for d in conn.devices}


def _nets(c: ToolCtx, work: str, lib: str, cell: str, db, prof):
    """The cell's nets (connectivity.Nets), extracted once per state of the layout: kept on the layout's database
    for its version (every edit, undo and reload makes a new one) and the schematic's connectivity, so a dry_run and
    the draw after it, a refused call and the next try, get_device_geometry and the route from its terminals extract
    once. Called under the layout's lock."""
    from undine.layouts import connectivity

    pins = _schematic_pins(c, work, lib, cell)
    key = (db.version, id(db.layout), cell, id(prof), json.dumps(pins, sort_keys=True))
    hit = getattr(db, "_nets_cache", None)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        nets = connectivity.Nets(db.layout, db.cell(cell), prof, pins)
    except ValueError as e:
        raise ToolError(str(e)) from e
    db._nets_cache = (key, nets)
    return nets


def _box_out(b, k: float) -> list[float]:
    return [round(b.left * k, 4), round(b.bottom * k, 4), round(b.right * k, 4), round(b.top * k, 4)]


def _shapes_out(region, k: float, polygons: bool, limit: int = 40):
    """A layer's shapes as an answer gives them: exact rectangles (a merged shape split into its fewest stripes, so a
    gate with its contact head reads as both), or with polygons each merged shape's outline [[x, y], ...]."""
    from undine.layouts import connectivity

    if polygons:
        out = [[[round(p.x * k, 4), round(p.y * k, 4)] for p in poly.each_point_hull()] for poly in region.merged().each()]
    else:
        out = [_box_out(b, k) for b in sorted(connectivity.boxes(region), key=lambda b: (b.left, b.bottom))]
    return out if len(out) <= limit else out[:limit] + [f"… {len(out) - limit} more ({len(out)} in all)"]


def _cut_arrays(region, k: float) -> dict:
    """Contacts / vias as arrays instead of one box each: their count and size, and per cluster (cuts a spacing
    apart) its columns, rows, pitch and extent. A cluster whose columns (rows) are not evenly spaced, such as a
    source's, a drain's and a tap's contact columns side by side, is irregular and lists where they are."""
    cuts = [p.bbox() for p in region.each()]
    sizes = sorted({(b.width(), b.height()) for b in cuts})
    reach = min((min(w, h) for w, h in sizes), default=0)        # cuts less than twice their size apart are one array
    groups = []
    for cl in region.sized(reach).merged().each():
        inside = [b for b in cuts if cl.bbox().contains(b.center())]
        xs, ys = sorted({b.left for b in inside}), sorted({b.bottom for b in inside})
        bb = inside[0]
        for b in inside[1:]:
            bb = bb + b
        g = {"count": len(inside), "columns": len(xs), "rows": len(ys), "bbox": _box_out(bb, k)}
        steps = [sorted({q - p for p, q in zip(v, v[1:])}) for v in (xs, ys)]
        if len(inside) > 1:
            g["pitch"] = [round(st[0] * k, 4) if st else 0 for st in steps]
        uneven = [len(st) > 1 for st in steps]
        if len(inside) != len(xs) * len(ys) or any(uneven):
            g["irregular"] = True
        for axis, v, u in (("x", xs, uneven[0]), ("y", ys, uneven[1])):
            if u:                                     # where its columns (rows) start
                g[axis] = [round(c * k, 4) for c in v[:12]] + ([f"… {len(v) - 12} more"] if len(v) > 12 else [])
        groups.append(g)
    groups.sort(key=lambda g: (g["bbox"][0], g["bbox"][1]))
    out = {"cuts": len(cuts), "size": [[round(w * k, 4), round(h * k, 4)] for w, h in sizes]}
    if len(out["size"]) == 1:
        out["size"] = out["size"][0]
    out["arrays"] = groups[:20] + ([f"… {len(groups) - 20} more ({len(groups)} in all)"] if len(groups) > 20 else [])
    return out


def _lvs_probe(c: ToolCtx, work: str, lib: str, cell: str, dbu: float):
    """The latest finished KLayout LVS of the cell as a probe: (what it is, probe(cut (layer, datatype), point in dbu)
    -> (layout net, schematic net)), or None. The deck's derived layers are anonymous in its database; the cuts it read
    keep their layer number, and every terminal has one."""
    import klayout.db as kdb
    from undine.checks import verify

    prj = c.srv.project(work)
    job = c.srv.jobs.latest(prj.path, kind="lvs", lib=lib, cell=cell, match=lambda st, d: st.get("tool") != "netgen")
    path = prj.path / "runs" / job["id"] / "result.lvsdb" if job else None
    if path is None or not path.is_file():
        return None
    lvs = kdb.LayoutVsSchematic()
    try:
        lvs.read(str(path))
    except Exception:
        return None
    layers = {}
    for n in lvs.layer_names():
        r = lvs.layer_by_name(n)
        info = lvs.layer_info(lvs.layer_index(r))
        if info.layer >= 0:
            layers.setdefault((info.layer, info.datatype), r)
    pairs: dict[str, str] = {}
    xref = lvs.xref()
    if xref is not None:
        for cp in xref.each_circuit_pair():
            first = verify._g(cp, "first")
            if first is not None and verify._g(first, "name") == cell:
                for np_ in xref.each_net_pair(cp):
                    a_, b_ = verify._g(np_, "first"), verify._g(np_, "second")
                    if a_ is not None:
                        pairs[a_.expanded_name()] = b_.expanded_name() if b_ is not None else None

    def probe(ld, p):
        r = layers.get(ld)
        net = lvs.probe_net(r, kdb.DPoint(p.x * dbu, p.y * dbu)) if r is not None else None
        return (net.expanded_name(), pairs.get(net.expanded_name())) if net is not None else None
    fresh = provenance.freshness(c.srv, work, job)
    return {"job": job["id"], "match": bool((job.get("summary") or {}).get("match")), "outdated": fresh["fresh"] is False}, probe


def get_device_geometry(c: ToolCtx, a: dict):
    """A placed device's shapes and terminals: its layers (any of the layout's, by name or layer/datatype; default every
    layer it draws) as exact rectangles or outlines, contacts as arrays, and its terminals (S / G / D / B, a MIM's
    plates) with their shapes, the net each is on in the layout now and in the latest LVS. `instance`: a schematic
    instance name (every piece bound to it: a device placed in pieces is all of them) or one piece's id (child@x,y,
    bound or not)."""
    import klayout.db as kdb
    from undine.layouts import connectivity
    from undine.layouts.layout import instance_ids

    work, lib, cell, db = _ldb(c, a, write=False)
    inst = str(a.get("instance") or "").strip()
    if not inst:
        raise ToolError("instance (the schematic instance name, e.g. M1, or a piece's id child@x,y) is required")
    prof = _profile(c, work)
    polygons = bool(a.get("polygons"))
    k = db.layout.dbu
    with db.lock:
        try:
            hits = _find_instances(db, cell, [inst])
        except ValueError:
            names = sorted({i["name"] for i in db.named_instances(cell)})
            raise ToolError(f"no layout instance bound to {inst} in {cell}"
                            + (f" (bound: {', '.join(names[:30])})" if names else " (nothing is bound: sync_layout places "
                               "the schematic's devices)") + "; or give a piece's id (child@x,y) as get_layout_info "
                            "lists them") from None
        by_name = any(h.get("name") == inst for h in hits)
        objs = dict((iid, i) for i, iid in instance_ids(db.layout, db.cell(cell)))
        pieces = [(h, objs[h["id"]]) for h in hits]
        names = _layer_names(db, prof)
        children = [db.layout.cell(o.cell_index) for _, o in pieces]
        if a.get("layers"):
            keys = list(dict.fromkeys(_layer_key(db, s_, cell, prof) for s_ in a["layers"]))
        else:
            keys = sorted({(db.layout.get_info(li).layer, db.layout.get_info(li).datatype) for li in db.layout.layer_indexes()
                           if any(not ch.bbox_per_layer(li).empty() for ch in children)})
        cuts = {prof.layers[x.name] for x in prof.stack.cuts if x.name in prof.layers} if prof and prof.stack else set()
        layers = {}
        for ld in keys:
            li = db.layout.find_layer(*ld)
            r = kdb.Region()
            for (_h, o), ch in zip(pieces, children):
                if li is not None:
                    r.insert(kdb.Region(ch.begin_shapes_rec(li)).transformed(next(iter(o.cell_inst.each_cplx_trans()))))
            num = f"{ld[0]}/{ld[1]}"
            label = f"{names[ld]} ({num})" if names.get(ld, num) != num else num        # "met1 (68/20)"; unnamed "68/20"
            if r.is_empty():
                if a.get("layers"):
                    layers[label] = []
                continue
            layers[label] = _cut_arrays(r, k) if ld in cuts and not polygons else _shapes_out(r, k, polygons)
        b = kdb.Box()
        for _h, o in pieces:
            b = b + o.bbox()
        cells = list(dict.fromkeys(ch.name for ch in children))
        out = {"instance": inst, "cell": cells[0] if len(cells) == 1 else cells, "bbox": _box_out(b, k), "unit": "um"}
        if len(pieces) > 1:
            out["pieces"] = [{"id": h["id"], "cell": h["cell"], "bbox": [round(v * k, 4) for v in h["bbox"]]} for h, _ in pieces]
        elif not by_name and hits[0].get("name"):
            out["bound_to"] = hits[0]["name"]
        out["layers"] = layers
        arr = pieces[0][1].cell_inst
        if len(pieces) == 1 and arr.is_regular_array():
            out["array"] = {"columns": arr.na, "rows": arr.nb, **_array_um({"columns": arr.na, "rows": arr.nb,
                                                                            "a": [arr.a.x, arr.a.y], "b": [arr.b.x, arr.b.y]}, k)}
        if a.get("terminals", True) and prof is not None and prof.stack is not None:
            nets = _nets(c, work, lib, cell, db, prof)
            dev = (nets.devices.get(inst) if by_name else nets.piece(hits[0]["id"])) or {}
            lvs = _lvs_probe(c, work, lib, cell, k) if dev.get("terminals") else None
            terms = {}
            tap = (dev.get("terminals", {}).get("B") or {}).get("layers", {}).get(prof.stack.tap) if prof.stack.tap else None
            split = []
            for t, v in (dev.get("terminals") or {}).items():
                key, more = v["key"], v.get("keys") or []
                e = {"schematic_net": dev["pins"].get(t.upper()), "net": nets.describe(key) if key is not None else None,
                     **({"nets": [f"{nets.describe(x)} at {_box_out(_on_net(nets, v['layers'], x).bbox(), k)}" for x in more]}
                        if len(more) > 1 else {}),
                     "shapes": {n: _shapes_out(r, k, polygons, 12) for n, r in v["layers"].items()
                                if n in nets.conductors or n == prof.stack.well}}
                if len(more) > 1:
                    split.append(t)
                if lvs is not None:
                    cut = next(((prof.layers[x.name], r) for x in prof.stack.cuts for n, r in v["layers"].items()
                                if n == x.name and not r.is_empty()), None)
                    got = lvs[1](cut[0], connectivity.inside(next(cut[1].each()))) if cut else None
                    if got:
                        e["lvs"] = {"layout_net": got[0], "schematic_net": got[1]}
                if t in ("S", "D") and tap is not None and not tap.is_empty() and \
                        v["layers"].get(prof.stack.diff, kdb.Region()).interacting(tap).count():
                    e["abuts_tap"] = True
                terms[t] = e
            if terms:
                out["device"] = {"kind": dev["kind"], "class": dev["class"], **dev["params"],
                                 **({"fingers": dev["fingers"]} if dev.get("fingers") else {})}
                if dev.get("swapped"):
                    out["device"]["note"] = ("S and D the other way round than the extraction's: named after the nets they "
                                             "are routed to (unrouted: the strip against the body tap is the bulk's net's)")
                out["terminals"] = terms
                if split:
                    out["terminals_split"] = (f"{', '.join(split)} {'is' if len(split) == 1 else 'are'} on more than one net "
                                              "(nets): the strips of its fingers" + (" or pieces" if len(pieces) > 1 else "")
                                              + " are not joined yet. route to 'INST:TERM' reaches the nearest of them; "
                                              "join the others too")
                if lvs is not None:
                    out["lvs"] = lvs[0]
            elif dev:
                out["terminals"] = None
                out["terminals_note"] = (f"{', '.join(cells)} {'holds' if len(cells) == 1 else 'hold'} "
                                         f"{'no device' if not dev.get('kind') else 'more than one device'} Undine recognises "
                                         "(a MOSFET, its fingers, a MIM capacitor)")
            short = {t: " / ".join(nets.names(v["key"])) or "unnamed" for t, v in (dev.get("terminals") or {}).items()
                     if v["key"] is not None}
            bad = [f"{inst}:{t} ({short.get(t)}) abuts its body tap ({short.get('B')}): a junction to another net "
                   "right against the tap" for t, e in terms.items()
                   if e.get("abuts_tap") and "B" in terms and short.get("B") != short.get(t)]
            if bad:
                out["warnings"] = bad
        elif a.get("terminals", True):
            out["terminals"] = None
            out["terminals_note"] = (f"terminals need the PDK's layer stack, which {prof.label if prof else 'this project'} "
                                     "has none of in Undine yet (sky130, GF180MCU and IHP SG13G2 have one): read the "
                                     "layers above instead")
    out["note"] = "um; rectangles [x1, y1, x2, y2] of the merged shapes." + (
        f" {inst} is {len(pieces)} pieces: the layers and terminals are of them all; give a piece's id for one." if len(pieces) > 1 else "") + (
        " A terminal's net: its labels, then the schematic nets of the device terminals on it; lvs: the latest LVS's. "
        "A MOSFET's S and D are named after the schematic nets they are routed to. route / add_via take 'INST:TERM' "
        "(INST a name or a piece's id)" if out.get("terminals") else "")
    return out


def _on_net(nets, layers: dict, key):
    """The shapes of a terminal (layers: {conductor: Region}) on one of its nets."""
    import klayout.db as kdb

    out = kdb.Region()
    for n, r in layers.items():
        if n in nets.conductors:
            out += r.interacting(nets.shapes(key, n))
    return out


def query_shapes(c: ToolCtx, a: dict):
    work, lib, cell, db = _ldb(c, a, write=False)
    k = db.layout.dbu
    box = [v / k for v in _box_um(a)]
    hits = db.select(cell, box, False, None, 0)
    want = None
    if a.get("layer"):
        try:
            want = db.layer_source(_lid(c, work, db, a["layer"], cell))
        except KeyError as e:
            raise ToolError(str(e).strip("'\"")) from e
    out = []
    for h in hits:
        if h["kind"] == "shape" and (want is None or h.get("layer") == want):
            out.append({"layer": h.get("layer"), "type": h["type"], "bbox_um": [round(v * k, 3) for v in h["bbox"]],
                        **({"text": h["text"]} if h["type"] == "text" else {})})       # a label: the net it names
        elif h["kind"] == "inst" and want is None:
            out.append({"instance_of": h.get("cell"), "bound_to": h.get("inst_name"), "bbox_um": [round(v * k, 3) for v in h["bbox"]]})
    return {"count": len(out), "items": out[:80], **({"truncated": True} if len(out) > 80 else {})}


def add_box(c: ToolCtx, a: dict):
    box = _box_um(a)
    if box[0] == box[2] or box[1] == box[3]:
        raise ToolError("the box has zero width or height")

    def fn(db, cell):
        lid = _lid(c, c.work, db, a.get("layer", ""), cell)
        k = db.layout.dbu
        db.create(cell, "box", lid, [v / k for v in box])
        return {"ok": True, **({"snapped_to": box} if _moved(box, a["box"]) else {})}
    return _lwrite(c, a, "add_box", fn)


def add_path(c: ToolCtx, a: dict):
    pts = a.get("points")
    if not isinstance(pts, list) or len(pts) < 2:
        raise ToolError("points must be [[x, y], [x, y], ...] in micrometres (at least two)")
    w = _um(a.get("width", 0))
    if w <= 0:
        raise ToolError("width (um) is required, e.g. 0.14 for met1")

    def fn(db, cell):
        lid = _lid(c, c.work, db, a.get("layer", ""), cell)
        k = db.layout.dbu
        flat = [_um(v) / k for p in pts for v in p]
        db.create(cell, "path", lid, flat, w / k)
        snapped = [[_um(x), _um(y)] for x, y in pts]
        return {"ok": True, **({"snapped_to": snapped} if _moved([v for p in snapped for v in p], [v for p in pts for v in p]) else {}),
                **({"width_snapped_to": w} if _moved([w], [a["width"]]) else {})}
    return _lwrite(c, a, "add_path", fn)


def add_label(c: ToolCtx, a: dict):
    text = (a.get("text") or "").strip()
    if not text:
        raise ToolError("text is required (the pin / net name)")

    def fn(db, cell):
        lid = _lid(c, c.work, db, a.get("layer", ""), cell)
        k = db.layout.dbu
        db.create(cell, "text", lid, [_um(a.get("x", 0)) / k, _um(a.get("y", 0)) / k], 0, text)
        return {"ok": True}
    return _lwrite(c, a, "add_label", fn)


def check_device_integrity(c: ToolCtx, a: dict):
    work, lib, cell, db = _ldb(c, a, write=False)
    k = db.layout.dbu
    names = [a["instance"]] if a.get("instance") else [i["name"] for i in db.named_instances(cell)]
    try:
        problems = [{"instance": p["instance"], "problem": p["problem"], "where_um": [round(v * k, 3) for v in p["where"]]}
                    for p in db.integrity(cell) if p["instance"] in names]
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    if a.get("instance") and a["instance"] not in {i["name"] for i in db.named_instances(cell)}:
        raise ToolError(f"no layout instance bound to {a['instance']}")
    return {"checked": names, "problems": problems,
            "advice": ("Shapes drawn in the cell overlap a device's transistor area, so LVS extracts a wider/longer gate (odd L / W values). "
                       "Delete or reshape them with delete_shapes / add_box so that poly crosses the diffusion only inside the device."
                       if problems else "no foreign poly over diffusion (or diffusion under poly) found")}


def delete_shapes(c: ToolCtx, a: dict):
    box = _box_um(a)

    def fn(db, cell):
        k = db.layout.dbu
        want = db.layer_source(_lid(c, c.work, db, a["layer"], cell)) if a.get("layer") else None
        hits = [h for h in db.select(cell, [v / k for v in box], False, None, 0) if h["kind"] == "shape" and (want is None or h.get("layer") == want)]
        if not hits:
            return {"ok": True, "deleted": 0, "note": "no shape lies completely inside the box"}
        if len(hits) > 40:
            raise ValueError(f"{len(hits)} shapes match; use a smaller box (max 40 per call)")
        db.delete(cell, [h["ref"] for h in hits])
        return {"ok": True, "deleted": len(hits)}
    return _lwrite(c, a, "delete_shapes", fn)


def _rot(a: dict, key: str = "rot") -> int:
    try:
        rot = int(float(a.get(key) or 0)) % 360
    except (TypeError, ValueError) as e:
        raise ToolError(f"{key} is 0, 90, 180 or 270 (degrees, counter-clockwise)") from e
    if rot not in ROTATIONS:
        raise ToolError(f"{key} is 0, 90, 180 or 270 (degrees, counter-clockwise)")
    return rot


def _find_instances(db, cell: str, wanted: list[str]) -> list[dict]:
    """The top-level instances `wanted` names: an id from get_layout_info (child@x,y, the origin in um with any
    decimals), or a schematic instance name (every instance bound to it). ValueError naming what was not found."""
    from undine.layouts.layout import ID_RE

    insts = db.instances(cell)
    k = db.layout.dbu

    def key(s: str):                            # an id as the grid has it: child, origin (dbu), #n
        m = ID_RE.fullmatch(str(s).strip())
        try:
            return m and (m.group(1), round(float(m.group(2)) / k), round(float(m.group(3)) / k), m.group(4) or "")
        except ValueError:                      # 1.2.3
            return None
    by_id = {key(i["id"]): i for i in insts}
    out, missing = [], []
    for w in wanted:
        hit = [by_id[key(w)]] if key(w) in by_id else [i for i in insts if i.get("name") == w]
        if not hit:
            missing.append(w)
        out += [i for i in hit if i not in out]
    if missing:
        raise ValueError(f"no layout instance {', '.join(missing)} in {cell}: name a schematic instance it is bound to, "
                         "or an id (child@x,y) as get_layout_info lists them")
    return out


def move_instance(c: ToolCtx, a: dict):
    if a.get("view") == "schematic":
        return _move_schematic(c, a)
    inst = a.get("instance")
    if not inst:
        raise ToolError("instance is required: the schematic instance it is bound to, or its id from get_layout_info "
                        "(child@x,y, also for an unbound instance)")
    absolute = a.get("x") is not None or a.get("y") is not None
    turn = a.get("rot") is not None or a.get("mirror") is not None
    if absolute and (a.get("x") is None or a.get("y") is None):
        raise ToolError("x and y go together: where the instance's origin goes, in um")
    if absolute and (a.get("dx") or a.get("dy")):
        raise ToolError("give x, y (where it goes) or dx, dy (by how much), not both")

    def fn(db, cell):
        import klayout.db as kdb

        k = db.layout.dbu
        hit = _find_instances(db, cell, [inst])
        if not absolute and not turn:
            made = db.move(cell, [h["ref"] for h in hit], _um(a.get("dx", 0)) / k, _um(a.get("dy", 0)) / k, False)
        else:
            if len(hit) > 1:
                raise ValueError(f"{inst} is {len(hit)} instances: move one at a time by its id ({', '.join(h['id'] for h in hit[:6])})")
            h = hit[0]
            old = kdb.Trans.from_s(h["trans"])
            rot = _rot(a) // 90 if a.get("rot") is not None else old.angle      # Trans.angle: in 90 degree steps
            mirror = bool(a["mirror"]) if a.get("mirror") is not None else old.is_mirror()
            if absolute:
                new = kdb.Trans(rot, mirror, int(round(_um(a["x"]) / k)), int(round(_um(a["y"]) / k)))
            else:                       # turned in place (the centre of its box stays where it was), then by dx, dy
                b = h["bbox"]
                centre = kdb.Point((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
                turned = kdb.Trans(rot, mirror, 0, 0)
                by = kdb.Vector(int(round(_um(a.get("dx") or 0) / k)), int(round(_um(a.get("dy") or 0) / k)))
                new = kdb.Trans(rot, mirror, centre - turned.trans(old.inverted().trans(centre)) + by)
            made = db.retransform(cell, h["ref"], new)
        refs = [m["ref"] for m in made if m.get("kind") == "inst"]
        return {"ok": True, "instances": [_inst_um(i, k) for i in db.instances(cell) if i["ref"] in refs][:10],
                "warning": "wires that were attached are not moved with it"}
    return _lwrite(c, a, "move_instance", fn)


def swap_instances(c: ToolCtx, a: dict):
    """Two layout instances exchanged in one recorded operation: their places (origin and orientation), or with what
    binding the schematic instances they are bound to. An id names a place (child@x,y), so swapping two bound units by
    two move_instance calls needed a free spot in between; this needs none."""
    pair = a.get("instances")
    if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(x, str) and x.strip() for x in pair):
        raise ToolError("instances is two layout instances: ids from get_layout_info (child@x,y) or the schematic "
                        "instances they are bound to (a device in one piece)")
    what = a.get("what") or "place"
    if what not in ("place", "binding"):
        raise ToolError("what is place (default: each goes where the other was, turned as it was) or binding (each stays "
                        "and takes the schematic instance the other is bound to)")

    def fn(db, cell):
        k = db.layout.dbu
        hits = []
        for x in pair:
            hit = _find_instances(db, cell, [x.strip()])
            if len(hit) > 1:
                raise ValueError(f"{x} is {len(hit)} instances (a device in pieces): name one piece by its id "
                                 f"({', '.join(h['id'] for h in hit[:6])})")
            hits.append(hit[0])
        if hits[0]["ref"] == hits[1]["ref"]:
            raise ValueError("the two instances are one: name two different ones")
        if what == "binding":
            if hits[0]["cell"] != hits[1]["cell"]:
                raise ValueError(f"only instances of one cell exchange bindings ({hits[0]['cell']} and {hits[1]['cell']} "
                                 "differ: the devices would take the other's size); swap their places instead")
            if not (hits[0].get("name") or hits[1].get("name")):
                raise ValueError("neither instance is bound to a schematic instance: nothing to exchange")
        made = db.swap(cell, hits[0]["ref"], hits[1]["ref"], binding=what == "binding")
        refs = [m["ref"] for m in made if m.get("kind") == "inst"]
        now = [_inst_um(i, k) for i in db.instances(cell) if i["ref"] in refs]
        out = {"ok": True, "what": what, "before": [_inst_um(h, k) for h in hits], "instances": now,
               "note": "ids name places: an instance's id is now the other's"}
        if what == "place":
            out["warning"] = "wires that were attached are not moved with them"
        return out
    return _lwrite(c, a, "swap_instances", fn)


def delete_instances(c: ToolCtx, a: dict):
    """Top-level instances out of a cell's layout: by id or binding (instances), inside a box, of a child cell; the
    criteria given all hold for each one deleted."""
    names, box, child = a.get("instances") or [], a.get("box"), (a.get("child") or "").strip()
    if not names and box is None and not child:
        raise ToolError("say which: instances (ids from get_layout_info, or schematic instance names: all their pieces), "
                        "box [x1, y1, x2, y2] (um: instances lying inside it) and / or child (instances of that cell)")
    b = _box_um(a) if box is not None else None

    def fn(db, cell):
        k = db.layout.dbu
        pool = _find_instances(db, cell, names) if names else db.instances(cell)
        if b is not None:
            q = [v / k for v in b]
            pool = [i for i in pool if q[0] <= i["bbox"][0] and q[1] <= i["bbox"][1] and i["bbox"][2] <= q[2] and i["bbox"][3] <= q[3]]
        if child:
            pool = [i for i in pool if i["cell"] == child]
        if not pool:
            return {"ok": True, "deleted": 0, "note": "no instance meets all of the criteria given"}
        db.delete(cell, [i["ref"] for i in pool])
        return {"ok": True, "deleted": len(pool), "instances": [_inst_um(i, k) for i in pool][:20],
                **({"more": len(pool) - 20} if len(pool) > 20 else {}),
                "note": "the wiring that went to them stays; a deleted device bound to the schematic is missing now "
                        "(sync_layout places it again)"}
    return _lwrite(c, a, "delete_instances", fn)


def place_pcell(c: ToolCtx, a: dict):
    """A PDK PCell variant with the parameters given, placed in a cell's layout: unbound (a dummy) or bound to a
    schematic instance (name: a piece of that device)."""
    from undine.layouts import sdl as sdlmod

    pc = (a.get("pcell") or "").strip()
    if not pc:
        raise ToolError("pcell is required: the PDK PCell, e.g. nfet, pfet, mim_cap, res_poly (an error names them all)")
    if a.get("x") is None or a.get("y") is None:
        raise ToolError("x and y are required: where the cell's origin goes, in um")
    params = a.get("params") or {}
    rot, name = _rot(a), (a.get("name") or "").strip() or None

    def fn(db, cell):
        k = db.layout.dbu
        prj = c.srv.project(c.work)
        if prj.pdk is None:
            raise ValueError("the project has no PDK")
        if (why := sdlmod.pcell.unavailable_reason(prj.pdk)):
            raise ValueError(why)
        cname = sdlmod.pcell_variant(prj.pdk, db, pc, params)
        placed = db.place(cell, cname, _um(a["x"]) / k, _um(a["y"]) / k, rot // 90, bool(a.get("mirror")), name,
                          own="place_pcell")
        i = next(x for x in db.instances(cell) if x["ref"] == placed[0]["ref"]) if placed else None
        return {"ok": True, **(_inst_um(i, k) if i else {"cell": cname})}
    res = _lwrite(c, a, "place_pcell", fn)
    if name and (why := _binding_note(c, a["lib"], a["cell"], name)):
        res["warning"] = why
    if not name:
        res["note"] = "not bound to a schematic instance: LVS sees its device (a dummy needs one in the schematic, or its terminals on one net)"
    return res


def _created(c: ToolCtx, cid: str | None) -> dict[str, dict[str, list[str]]]:
    """What changeset `cid` created that its working copy still has: {lib: {cell: [views]}} (layout, schematic,
    symbol), from its own records, undone ones too (an undone create_cell leaves its empty cell): a cell's creation
    (create_cell, sync_layout's PCell variants and the _par / _tie cells round them, place_pcell's) and a view's file
    created (create_cell, import). The cells of the baseline are never in it. {} without an open changeset."""
    if not cid:
        return {}
    try:
        meta = c.cs._load(c.project, cid)
        if meta.get("status") != "open":
            return {}
        recs = c.cs._all_records(meta)
        work = meta["preview"]
        prj = c.srv.project(work)
    except (KeyError, ValueError, OSError):
        return {}
    made: dict[str, dict[str, set]] = {}
    for lib, r in recs:
        if r.get("op") == "restore":                 # what a restore brings back was made (or was there) before
            continue
        for e in r.get("effects") or []:
            if e.get("k") == "c" and e.get("do") == "add" and not e.get("kept") and e.get("cell"):
                made.setdefault(lib, {}).setdefault(e["cell"], set()).add("layout")
            elif e.get("k") == "v" and e.get("do") == "add" and not e.get("kept") and r.get("cell") \
                    and r.get("view") in ("schematic", "symbol"):
                made.setdefault(lib, {}).setdefault(r["cell"], set()).add(r["view"])
    out: dict[str, dict[str, list[str]]] = {}
    for lib, cells in made.items():
        try:
            library = prj.library(lib)
        except KeyError:
            continue
        db = c.srv.db(work, lib) if "layout" in {v for vs in cells.values() for v in vs} else None
        for cell, views in cells.items():
            have = [v for v in ("layout", "schematic", "symbol") if v in views and (
                (db is not None and (x := db.layout.cell(cell)) is not None and not x.is_ghost_cell()) if v == "layout"
                else (library.path / f"{cell}.{'sch' if v == 'schematic' else 'sym'}").is_file())]
            if have:
                out.setdefault(lib, {})[cell] = have
    return out


def _symbol_users(c: ToolCtx, work: str, lib: str, cell: str) -> list[str]:
    """The schematics of the project (testbenches too) that place lib/cell's symbol."""
    prj = c.srv.project(work)
    users = []
    for name in prj.library_specs():
        try:
            path = prj.library(name).path
        except KeyError:
            continue
        for sch in sorted(path.glob("*.sch")):
            if sch.parent == prj.library(lib).path and sch.stem == cell:
                continue
            try:
                comps = xschem.parse(sch.read_text(errors="replace")).comps
            except Exception:
                continue
            for comp in comps:
                parts = comp.sym.replace("\\", "/").split("/")
                if parts[-1] == f"{cell}.sym" and (len(parts) < 2 or parts[-2] == lib or sch.parent == prj.library(lib).path):
                    users.append(f"{name}/{sch.stem}")
                    break
    return users


DELETABLE_VIEWS = ("layout", "schematic", "symbol")


def delete_cell(c: ToolCtx, a: dict):
    """A cell's views (layout, schematic, symbol) out of a library of the changeset: each recorded with its content
    (a layout cell's shapes and instances, a file's text), so restore_changeset (and undo) brings it back. Refused while
    anything places it: the layout cell in another cell's layout, the symbol in a schematic (a testbench too), the
    schematic while its symbol stays and is placed."""
    with c.srv.changeset_lock:
        return _delete_cell(c, a)


def _delete_cell(c: ToolCtx, a: dict):
    lib, cell = str(a.get("lib") or "").strip(), str(a.get("cell") or "").strip()
    if not lib or not cell:
        raise ToolError("lib and cell are required: the cell whose views go")
    dry = bool(a.get("dry_run"))
    work = c.work if dry else c.need_changeset()
    try:
        prj = c.srv.project(work)
        library = prj.library(lib)
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    if library.readonly:
        raise ToolError(f"library {lib} is read-only (a dependency or the PDK): its cells are not deleted here")
    files = {"schematic": library.path / f"{cell}.sch", "symbol": library.path / f"{cell}.sym"}
    db = c.srv.db(work, lib) if library.has_layout() or (work, lib) in getattr(c.srv, "dbs", {}) else None
    has = {"schematic": files["schematic"].is_file(), "symbol": files["symbol"].is_file(),
           "layout": db is not None and (x := db.layout.cell(cell)) is not None and not x.is_ghost_cell()}
    views = a.get("views")
    if views is None:
        views = [v for v in DELETABLE_VIEWS if has[v]]
        if not views:
            raise ToolError(f"{lib}/{cell} has no layout, schematic or symbol view: list_cells shows the cells")
    if not isinstance(views, list) or not views or set(views) - set(DELETABLE_VIEWS):
        raise ToolError("views is a list of layout, schematic and / or symbol (default: every one the cell has)")
    views = [v for v in DELETABLE_VIEWS if v in views]
    if (missing := [v for v in views if not has[v]]):
        raise ToolError(f"{lib}/{cell} has no {' and no '.join(missing)} view")
    why: list[str] = []
    if "layout" in views:
        with db.lock:
            users = sorted({db.layout.cell(p).name for p in db.layout.cell(cell).each_parent_cell()})
        if users:
            why.append(f"its layout is placed in {', '.join(users[:6])}{' ...' if len(users) > 6 else ''} "
                       "(delete_instances with child there first)")
    sym_users = _symbol_users(c, work, lib, cell) if has["symbol"] else []
    if "symbol" in views and sym_users:
        why.append(f"its symbol is placed in {', '.join(sym_users[:6])}{' ...' if len(sym_users) > 6 else ''}")
    elif "schematic" in views and "symbol" not in views and sym_users:
        why.append(f"its symbol stays and is placed in {', '.join(sym_users[:6])}: those would lose the circuit "
                   "behind it (delete the symbol too once nothing places it)")
    if why:
        raise ToolError(f"{lib}/{cell} is not deleted: " + "; ".join(why))
    created = _created(c, c.changeset).get(lib, {}).get(cell, [])
    out: dict = {"cell": f"{lib}/{cell}", "views": views,
                 "made_by_this_changeset": [v for v in views if v in created]}
    if "layout" in views:
        with db.lock:
            kids = sorted({db.layout.cell(k).name for k in db.layout.cell(cell).each_child_cell()})
        if kids:
            out["children_left"] = kids[:12] + ([f"… {len(kids) - 12} more"] if len(kids) > 12 else [])
    left = [v for v in DELETABLE_VIEWS if has[v] and v not in views]
    intent_file = library.path / f"{cell}.intent.yaml"
    if dry:
        return {"dry_run": True, **out, "note": "nothing was deleted; call again without dry_run"}
    _check_limit(c)
    if not (a.get("reason") or "").strip():
        raise ToolError("reason is required: say why this change is made")
    why_ = a["reason"]
    # every view checked above; the parts are recorded in this one tool call's operation (one restore point), and
    # if one fails the ones before it are undone, so the cell keeps all its views
    done: list = []
    try:
        if "layout" in views:
            with db.recording("delete_cell", {"cell": cell, "view": "layout"}, c.actor, why_, c.changeset):
                db.drop_cells([cell])
            done.append((db, "layout"))
        for v in ("symbol", "schematic"):           # the symbol first: it takes its pins from the schematic
            if v in views:
                ed = c.srv.editor(work, lib, cell, v)
                with ed.recording("delete_cell", {"cell": cell, "view": v}, c.actor, why_, c.changeset):
                    ed.delete_file()
                done.append((ed, v))
    except (KeyError, ValueError, PermissionError) as e:
        undone = []
        for part, v in reversed(done):
            try:
                with part.recording("delete_cell", {"cell": cell, "view": v}, c.actor, why_, c.changeset):
                    undone.append(bool(part.undo()))
            except Exception:
                undone.append(False)
        msg = str(e).strip("'\"")
        if done and not all(undone):
            raise ToolError(f"{msg}; the views deleted before it could not all be brought back: get_changeset shows "
                            "what is left, restore_changeset to the point before this call takes it back") from e
        raise ToolError(msg + ("; the views deleted before it were brought back: nothing was deleted" if done else "")) from e
    c.writes += 1
    notes = ["recorded with its content: restore_changeset to a point before this brings it back"]
    if out.get("children_left"):
        notes.append("the cells it placed stay (delete_unused_cells takes generated ones nothing places any more)")
    if left:
        notes.append(f"its {' and '.join(left)} stay{'s' if len(left) == 1 else ''}")
    elif intent_file.is_file():
        notes.append(f"its design intent ({intent_file.name}) stays")
    return {"ok": True, **out, "note": "; ".join(notes)}


def delete_unused_cells(c: ToolCtx, a: dict):
    """The PCell variants Undine generated (and sync_layout's cells around them) that nothing in the library places
    any more, out of a library's layout: recorded as one operation (a record per cell, its content in it), so undo and
    restore_changeset bring them back. Only such cells: a cell of the designer's, or one placed anywhere, is refused.
    Without cells, those this changeset created (_created); the baseline's (left by changesets applied before) only
    with include_baseline, and are counted otherwise."""
    from undine.layouts import sdl as sdlmod

    lib = str(a.get("lib") or "").strip()
    if not lib:
        raise ToolError("lib is required: the library whose unused generated cells go")
    dry = bool(a.get("dry_run"))
    work = c.work if dry else c.need_changeset()
    try:
        db = c.srv.db(work, lib)
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    path = c.srv.project(work).library(lib).path
    with db.lock:
        unused = [n for n in sdlmod.unused_generated(db.layout)
                  if not any((path / f"{n}.{x}").is_file() for x in ("sch", "sym"))]
        gen = sdlmod.generated_cells(db.layout)
        want = [str(n) for n in a.get("cells") or []]
        if want:
            why = {}
            for n in want:
                cc = db.layout.cell(n)
                if cc is None or cc.is_ghost_cell():
                    why[n] = "no such cell"
                elif n not in gen:
                    why[n] = "not a cell Undine generated for a PCell"
                elif n not in unused:
                    users = sorted({db.layout.cell(p).name for p in cc.each_parent_cell()})
                    why[n] = f"placed in {', '.join(users[:4])}" if users else "has a schematic or symbol"
            if why:
                raise ToolError("not deleted: " + "; ".join(f"{n}: {w}" for n, w in why.items()) +
                                ". delete_unused_cells takes only generated PCell cells nothing places (without cells: those this "
                                "changeset made; a cell of the designer's goes with delete_cell)")
            pick = [n for n in unused if n in want]
            if (left := sorted(n for n in pick if any(db.layout.cell(p).name not in pick for p in db.layout.cell(n).each_parent_cell()))):
                raise ToolError(f"{', '.join(left)} {'is' if len(left) == 1 else 'are'} placed in a cell not deleted with "
                                f"{'it' if len(left) == 1 else 'them'}: name that cell too")
        else:
            mine = set(_created(c, c.changeset).get(lib, {}))
            pick = unused if a.get("include_baseline") else [n for n in unused if n in mine]
            while (held := {n for n in pick if any(db.layout.cell(p).name not in pick
                                                   for p in db.layout.cell(n).each_parent_cell())}):
                pick = [n for n in pick if n not in held]    # one of ours a baseline cell places stays with it
    baseline = [n for n in unused if n not in mine] if not want else []
    scope = {}
    if not want:
        scope = {"scope": "every unused generated cell" if a.get("include_baseline") else "the ones this changeset created"}
        if baseline and not a.get("include_baseline"):
            scope["baseline_unused"] = len(baseline)
            scope["baseline_note"] = (f"{len(baseline)} unused generated cell{'s' if len(baseline) != 1 else ''} of the "
                                      "baseline (there before this changeset) left alone: include_baseline: true takes "
                                      "them too, or name them in cells (dry_run lists what would go)")
            scope["baseline_cells"] = baseline[:12] + ([f"… {len(baseline) - 12} more"] if len(baseline) > 12 else [])
    if not pick:
        return {"ok": True, "deleted": [], **scope,
                "note": f"{lib} has no generated PCell cell {'' if a.get('include_baseline') or want else 'of this changeset '}"
                        "that nothing places"}
    if dry:
        return {"dry_run": True, "would_delete": pick, **scope, "note": "nothing was deleted; call again without dry_run"}
    _check_limit(c)
    if not (a.get("reason") or "").strip():
        raise ToolError("reason is required: say why this change is made")
    try:
        with db.recording("delete_unused_cells", {"cells": pick}, c.actor, a["reason"], c.changeset):
            db.drop_cells(pick)
    except (KeyError, ValueError, PermissionError) as e:
        raise ToolError(str(e).strip("'\"")) from e
    c.writes += 1
    return {"ok": True, "deleted": pick, **scope,
            "note": "recorded: restore_changeset to the point before this brings them back; run_checks no longer lists "
                    "them (it skips unused generated cells anyway)"}


def place_array(c: ToolCtx, a: dict):
    from undine.layouts import sdl as sdlmod

    devices = a.get("devices")
    if not devices and a.get("pattern") != "rows":
        raise ToolError("devices is required: the schematic instances whose units go into the array, e.g. "
                        "['C0', 'C1', ..., 'C9'], or {name: units} to split MOSFETs into that many one-finger units")
    origin = a.get("origin")
    if not isinstance(origin, list) or len(origin) != 2:
        raise ToolError("origin is [x, y] in um: the array's lower-left corner")
    pitch = a.get("pitch")
    if pitch is not None and (not isinstance(pitch, list) or len(pitch) != 2):
        raise ToolError("pitch is [x, y] in um (default: the PDK's rules)")

    def fn(db, cell):
        prj = c.srv.project(c.work)
        sch = prj.library(a["lib"]).path / f"{cell}.sch"
        if not sch.is_file():
            raise KeyError(f"no schematic {a['lib']}/{cell}: the units are bound to its devices")
        sd = sdlmod.Sdl(prj.pdk, db, sch, xschem.SymbolResolver(prj.symbol_paths()))
        return sd.place_array(devices or [], a.get("pattern") or "common_centroid", a.get("rows"),
                              _whole(a, "columns") if a.get("columns") else None,
                              tuple(_um(v) for v in pitch) if pitch else None, tuple(_um(v) for v in origin),
                              int(a.get("dummies") or 0), a.get("dummy") or None, bool(a.get("replace")))
    return _lwrite(c, a, "place_array", fn, need=False)


def restore_changeset(c: ToolCtx, a: dict):
    """The working copy back to a restore point of its history (get_changeset's restore_points), recorded as a
    restore: the edits after it are taken back, kept in the history."""
    c.need_changeset()
    if a.get("to") is None:
        raise ToolError("to is required: a restore point (after) from get_changeset's restore_points, or 0: as opened")
    try:
        to = int(a["to"])
    except (TypeError, ValueError) as e:
        raise ToolError("to is a whole number: a restore point from get_changeset") from e
    try:
        c.cs.restore(c.project, c.changeset, to, c.actor)
    except ChangesetError as e:
        raise ToolError(str(e)) from e
    c.writes += 1
    return {"ok": True, "restored_to": to, "restore_points": _restore_points(c, c.changeset)[0],
            "note": "the operations after it are taken back (the history keeps them); run_checks again"}


RESTORE_POINTS = 12      # restore points get_changeset lists by default (restore_points: n | 'all', restore_before)


def _restore_points(c: ToolCtx, cid: str, last: int | None = RESTORE_POINTS, before: int | None = None,
                    running: int | None = None) -> tuple[list[dict], int, int]:
    """The operations of an open changeset, newest first, each with the restore point right after it
    (restore_changeset(to=after)); the newest is where it is now. `before`: only those whose point is before that one;
    `last`: at most that many (None: all); `running`: the head before a batch still being applied (its records are
    no operation yet). Returns (them, how many older ones are left out, the point it is at now: 0 as opened)."""
    t = c.cs.timeline(c.project, cid)
    ops = [o for o in t["ops"] if running is None or o["first"] <= running]
    out = [{"after": o["last"], "what": ", ".join(o["ops"]), "cells": o["cells"][:4], "why": (o["reasons"] or [""])[0][:120],
            **({"undone": True} if o["kind"] == "edit" and not o["in_effect"] else {}),
            **({"restored_to": o["to"]} if o["kind"] == "restore" else {})} for o in ops]
    now = out[-1]["after"] if out else 0
    out = [o for o in reversed(out) if before is None or o["after"] < before]
    return ((out, 0) if last is None else (out[:last], max(0, len(out) - last))) + (now,)


# ---------------------------------------------------------------------------------- drawing on the stack (drawing.py)
# Vias, routes, taps and guard rings, rule-correct from the PDK's stack, refused when they would touch another net
# (connectivity.Nets: labels and the device terminals name the nets) or come closer than a spacing to another shape.


def _plan_or_draw(c: ToolCtx, a: dict, op: str, plan):
    """A drawing tool: plan(db, cell, rules, nets) -> (shapes [(stack layer, box)], answer). Drawn as one operation in
    the changeset; with dry_run the answer says what would be drawn and nothing is (it needs no changeset)."""
    from undine.layouts import drawing

    def planned(db, cell, work):
        prof = _profile(c, work)
        if prof is None or prof.stack is None:
            raise ToolError(f"{op} needs the PDK's layer stack and rules: {prof.label if prof else 'this project'} has "
                            "none in Undine yet (sky130, GF180MCU and IHP SG13G2 have). Draw with add_box / add_path and "
                            "check with run_checks")
        try:
            r = drawing.Rules(prof, db.layout.dbu)
            shapes, out = plan(db, cell, r, _nets(c, work, a["lib"], cell, db, prof))
        except drawing.DrawError as e:
            raise ToolError(str(e)) from e
        out["drawn"] = _drawn(shapes)
        return [(prof.layers[n], b) for n, b in shapes], out

    if a.get("dry_run"):
        work, _lib, cell, db = _ldb(c, a, write=False)
        with db.lock:
            _, out = planned(db, cell, work)
        return {"dry_run": True, **out, "note": "nothing was drawn; call again without dry_run to draw it"}

    def fn(db, cell):
        shapes, out = planned(db, cell, c.work)
        db.draw(cell, shapes)
        return {"ok": True, **out}
    return _lwrite(c, a, op, fn)


def _drawn(shapes: list) -> dict[str, int]:
    """What a drawing tool draws, counted per layer."""
    out: dict[str, int] = {}
    for n, _ in shapes:
        out[n] = out.get(n, 0) + 1
    return out


def _problems(nets, r, shapes: list, allowed: set | None, net: str | None, where: list | None = None) -> list[str]:
    """What drawing `shapes` would do wrong, said where: touch a net it must not (allowed: the nets a route joins, and
    nets called `net`; None: a via, tap or ring may touch nets called `net` and unnamed ones, or without `net` nets of
    one name), or come closer to another shape than its layer's spacing (a cut's, and the keep-away of a contact).
    `where` gets the boxes (dbu) of the shapes in the way."""
    import klayout.db as kdb

    k = r.dbu
    by_layer: dict[str, kdb.Region] = {}
    for n, b in shapes:
        by_layer.setdefault(n, kdb.Region()).insert(b)
    touched: dict = {}
    for n, reg in by_layer.items():
        if n in nets.R:
            for key, hit in nets.touching(n, reg.merged()).items():
                touched.setdefault(key, (n, hit))
    want = (net or "").lower()
    named = {key: {x.lower() for x in nets.names(key)} for key in touched}
    if allowed is not None:
        bad = [key for key in touched if key not in allowed and not (want and want in named[key])]
    elif want:
        bad = [key for key in touched if named[key] and want not in named[key]]
    else:
        groups = [v for v in named.values() if v]
        joined = groups and not set.intersection(*groups)
        bad = [key for key in touched if named[key]] if joined else []
    out = [f"touches {nets.describe(key)} on {touched[key][0]} at {_box_out(touched[key][1].bbox(), k)}" for key in bad]
    if where is not None:
        where += [touched[key][1].bbox() for key in bad]
    if allowed is None and not want and bad:
        out.insert(0, "it would join " + " and ".join(sorted({nets.describe(key) for key in bad})))
    cuts = {x.name: x for x in r.st.cuts}
    for n, reg in by_layer.items():
        if n not in nets.R:
            continue
        cond = r.st.conductor(n)
        space = cond.space if cond else cuts[n].space if n in cuts else None
        if space:
            for wide in [None, *(cond.wide if cond else ())]:     # the spacing, and next to a wide shape each level's
                found = nets.near(n, reg.merged(), r.d(space), (r.d(wide[0]), r.d(wide[1])) if wide else None)
                for p in found[:4]:
                    if where is not None:
                        where.append(p.bbox())
                    big = f" ({wide[1]:g} um where one is wider than {wide[0]:g} um)" if wide else ""
                    out.append(f"comes closer than {space:g} um{big} to a {n} shape at {_box_out(p.bbox(), k)}")
                if found:
                    break
            # a notch the new shapes leave in what they join on their own net (a pad beside another pad on one strap):
            # the decks check it as a spacing too, though no other shape is there
            mine = reg.merged()
            joined = (nets.window(n, mine.bbox().enlarged(r.d(space), r.d(space))) + mine).merged()
            for ep in list(joined.notch_check(r.d(space)).each())[:20]:
                gap = kdb.Region(ep.polygon(0))                 # between the two edges: one of them the new shapes'
                if not gap.sized(1).interacting(mine).is_empty() and not (gap - mine).is_empty():
                    if where is not None:
                        where.append(gap.bbox())
                    out.append(f"leaves a notch narrower than {space:g} um in its own net's {n} at {_box_out(gap.bbox(), k)}")
        if cond is not None and cond.away:              # poly: never a new gate, kept from diffusion and tap
            added = reg.merged() - nets.window(n, reg.bbox().enlarged(1, 1))
            for lay, sp in cond.away if not added.is_empty() else ():
                for p in list(nets.window(lay + "$raw", added.bbox().enlarged(1, 1)).interacting(added).each())[:2]:
                    if where is not None:
                        where.append(p.bbox())
                    out.append(f"its {n} would cross {lay} at {_box_out(p.bbox(), k)}"
                               + (" (a new transistor)" if lay == r.st.diff else ""))
                for p in nets.near(lay + "$raw", added, r.d(sp))[:2]:
                    if where is not None:
                        where.append(p.bbox())
                    out.append(f"its {n} comes closer than {sp:g} um to {lay} at {_box_out(p.bbox(), k)}")
    for x in r.st.cuts:                                # a contact on poly, or on a diffusion: those it sits on
        if x.name in by_layer and x.away:
            under = by_layer.get(x.below, kdb.Region()) + nets.window(x.below, by_layer[x.name].bbox().enlarged(1, 1))
            mine = by_layer[x.name].interacting(under.merged())
            for lay, sp in x.away if not mine.is_empty() else ():
                if lay in nets.R:
                    for p in nets.near(lay, mine, r.d(sp))[:2] + list(nets.window(lay, mine.bbox()).interacting(mine).each())[:2]:
                        out.append(f"its {x.name} comes closer than {sp:g} um to {lay} at {_box_out(p.bbox(), k)} ({x.rules})")
    for x in r.st.cuts:                                # a cut on or against one already there, of any net: the two
        if x.name not in by_layer or x.name not in nets.R:     # would be one shape, no cut's size (via.1a, ct.1)
            continue
        for b in dict.fromkeys(p.bbox() for p in by_layer[x.name].each()):
            for p in nets.window(x.name, b.enlarged(1, 1)).each():
                if p.is_box() and p.bbox() == b:       # exactly on it: the same cut
                    continue
                if (kdb.Region(p) & kdb.Region(b.enlarged(1, 1))).is_empty():
                    continue                           # only near it: the spacing above says so
                if where is not None:
                    where.append(p.bbox())
                out.append(f"its {x.name} at {_box_out(b, k)} overlaps or abuts a {x.name} already there at "
                           f"{_box_out(p.bbox(), k)}: the two would be one shape, not a cut" + (f" ({x.rules})" if x.rules else "")
                           + "; put it exactly "
                           f"on that one, or {x.space:g} um clear of it")
                break
    if r.profile.magic_drc:
        out += _magic_cuts(nets, r, by_layer, where)
    return list(dict.fromkeys(out))


def _subcell_shapes(nets, layer: str, box, own: bool = False) -> "kdb.Region":
    """The shapes of a stack layer inside the instances of the cell (not its own), unmerged, that meet a box; with
    `own`, the cell's own instead."""
    import klayout.db as kdb

    li = nets.li.get(layer)
    if li is None:
        return kdb.Region()
    it = kdb.RecursiveShapeIterator(nets.layout, nets.top, li, box, False)
    if own:
        it.max_depth = 0
    else:
        it.min_depth = 1
    return kdb.Region(it)


def _contacts(cuts, grow: int, close: int) -> "kdb.Region":
    """Cuts as Magic reads them (its cifinput): each grown by the metal's enclosure, and those that then come within
    twice `close` of each other (an array's at its spacing) one contact region."""
    import klayout.db as kdb

    grown = kdb.Region([p.bbox().enlarged(grow, grow) for p in cuts.each()])
    return grown.sized(close).sized(-close) if close > 0 else grown.merged()


def _magic_cuts(nets, r, by_layer: dict, where: list | None) -> list[str]:
    """What Magic refuses of new vias next to the vias of a placed cell (a PCell's), which the PDK's KLayout deck
    passes. Magic reads a via as a contact the size of its cut grown by the metal's enclosure (sky130: via3 0.06 um,
    via4 0.19 um), the cuts of an array at their spacing merged into one contact region; a contact region may not abut
    or partially overlap one of its kind in a subcell ("This layer can't abut or partially overlap between subcells"):
    it is the same region exactly, or clear of it. A capacitor's own contacts (the cut on its plate, sky130's via3 on
    capm) stack with no via at all, so a via of the levels next to them over them is "Can't overlap those layers"."""
    import klayout.db as kdb

    k = r.dbu
    front = {r.st.diff, r.st.tap, r.st.poly}
    cuts = {c.name: c for c in r.st.cuts if c.below not in front}
    out = []
    for name, reg in by_layer.items():
        c = cuts.get(name)
        if c is None:
            continue
        g = r.d(c.enc_below[0])
        close = max(0, (r.d(c.space) - 2 * g) // 2)
        reach = reg.bbox().enlarged(g + 2 * close + r.d(1.0), g + 2 * close + r.d(1.0))
        new = kdb.Region([p.bbox() for p in reg.each()])
        span = reach
        for _ in range(2):                                             # the whole of an array it meets
            mine = _contacts(new + _subcell_shapes(nets, name, span, own=True), g, close).interacting(_contacts(new, g, 0))
            raw = _subcell_shapes(nets, name, span)
            for pl in r.st.plates:                                     # a capacitor's own (below): another kind to Magic
                if pl.cut == name:
                    raw = raw.not_inside(_subcell_shapes(nets, pl.name, span).merged())
            theirs = _contacts(raw, g, close)
            box = (mine + theirs.interacting(mine)).bbox().enlarged(2 * close + 1, 2 * close + 1) + reach
            if box == span:
                break
            span = box
        for p in mine.each():                                          # one of its kind in a subcell
            hit = [q for q in theirs.interacting(kdb.Region(p)).each() if not (kdb.Region(q) ^ kdb.Region(p)).is_empty()]
            if not hit:
                continue
            b = hit[0].bbox()
            if where is not None:
                where.append(b)
            out.append(f"its {name} abuts or partly overlaps a {name} of a placed cell at {_box_out(b, k)}, which Magic "
                       f"refuses (it reads a via as a contact {c.enc_below[0]:g} um round its cut, an array's cuts as one "
                       "contact: 'can't abut or partially overlap between subcells'): put the same via (the same array) "
                       "exactly on it, or clear of it")
            break
        for pl in r.st.plates:                                         # a capacitor's own contacts
            pc = next((x for x in r.st.cuts if x.name == pl.cut), None)
            if pc is None:
                continue
            up = next((x.name for x in r.st.cuts if x.below == pc.above), None)
            if name not in {pl.cut, up, next((x.name for x in r.st.cuts if x.above == pc.below), None)}:
                continue
            gp = r.d(pc.enc_below[0])
            theirs = _subcell_shapes(nets, pl.cut, reach) & _subcell_shapes(nets, pl.name, reach)
            hit = [p.bbox() for p in theirs.each()
                   if not kdb.Region(p.bbox().enlarged(gp + g + 1, gp + g + 1)).interacting(reg).is_empty()]
            if hit:
                box = kdb.Box()
                for x in hit:
                    box = box + x
                if where is not None:
                    where.append(box)
                out.append(f"its {name} comes onto the {pl.cut} of a capacitor's {pl.name} at {_box_out(box, k)}: Magic "
                           "refuses any other via over or against a capacitor's own contacts ('Can't overlap those layers'; "
                           f"contacts {c.enc_below[0]:g} and {pc.enc_below[0]:g} um round their cuts). Land the {name} on "
                           f"{pc.above if name == up else 'the metal'} clear of the {pl.cut} array (outside the {pl.name})")
    return out


def _bridged(nets, r, shapes: list) -> tuple[list, list, list[str]]:
    """A cut's extra layer among `shapes` (sky130's npc round a poly contact) bridged to the shapes of its kind in the
    layout closer than its spacing, as the decks want them merged (npc.2: a neighbouring gate's contact, a PCell's
    own): (the shapes with the fills, the fills, what cannot be so: a gap no fill closes, or a fill closer to a
    diffusion or tap than the contact keeps from them less its enclosure, so near their contacts: licon.13)."""
    import klayout.db as kdb
    from undine.layouts import drawing

    out, fills, problems, k = list(shapes), [], [], r.dbu
    extras = {e[0]: (c, e) for c in r.st.cuts for e in c.extra}
    for name, (c, (_, enc, width, space)) in extras.items():
        mine = kdb.Region([b for n, b in shapes if n == name]).merged()
        if mine.is_empty():
            continue
        sp = r.d(space)
        around = nets.window(name, mine.bbox().enlarged(sp, sp)).interacting(mine.sized(sp - 1))
        if around.is_empty():
            continue
        got, bad = drawing.bridge(r, mine, around, r.d(width), sp)
        for b in bad[:1]:
            problems.append(f"its {name} comes closer than {space:g} um to the {name} at {_box_out(b, k)} where no fill "
                            f"bridges them (npc.1 / npc.2: merged, at least {width:g} um wide)")
        if got:
            fill = kdb.Region(got)
            for lay, d in c.away:
                keep = r.d(d - enc)
                hits = nets.near(lay + "$raw", fill, keep)[:2] + \
                    list(nets.window(lay + "$raw", fill.bbox()).interacting(fill).each())[:2]
                for p in hits:
                    problems.append(f"the {name} bridging it to the {name} beside it comes closer than {d - enc:g} um to "
                                    f"{lay} at {_box_out(p.bbox(), k)} ({c.rules})")
        out += [(name, b) for b in got]
        fills += [(name, b) for b in got]
    return out, fills, problems


def _refuse(what: str, problems: list[str], hint: str = "") -> ToolError:
    return ToolError(f"{what} not drawn: " + "; ".join(problems[:8]) + (f" (and {len(problems) - 8} more)" if len(problems) > 8 else "")
                     + (f". {hint}" if hint else ""))


def _dir(a: dict) -> bool | None:
    d = (a.get("direction") or "").lower()
    if d and d not in ("h", "v", "horizontal", "vertical"):
        raise ToolError("direction is h (the larger enclosures left and right) or v (bottom and top)")
    return None if not d else d.startswith("h")


def add_via(c: ToolCtx, a: dict):
    """A rule-correct via stack between two layers of the stack at x, y (um): cut arrays, the pads each conductor needs
    (enclosures, minimum width and area), refused when it would join two nets or comes too close to a shape."""
    import klayout.db as kdb

    if a.get("x") is None or a.get("y") is None or not a.get("from_layer") or not a.get("to_layer"):
        raise ToolError("x, y, from_layer and to_layer are required (e.g. from_layer li1, to_layer met2)")
    x, y = _um(a["x"]), _um(a["y"])
    size = a.get("size")
    if size is not None and (not isinstance(size, list) or len(size) != 2 or min(_um(v) for v in size) <= 0):
        raise ToolError("size is [w, h] in um: the footprint to fill with cuts")
    horizontal = _dir(a)
    frm, to = a["from_layer"], a["to_layer"]

    def plan(db, cell, r, nets):
        from undine.layouts import drawing

        px, py = r.snap(r.d(x)), r.snap(r.d(y))
        h = horizontal
        if h is None:                                  # along the shape it lands on, else the footprint's long side
            here = nets.window(frm, kdb.Box(px - 1, py - 1, px + 1, py + 1)) if frm in nets.R else kdb.Region()
            hit = next((p.bbox() for p in here.each() if p.bbox().contains(kdb.Point(px, py))), None)
            h = hit.width() >= hit.height() if hit else (size is None or size[0] >= size[1])
        res = drawing.via_stack(r, px, py, frm, to, a.get("rows"), a.get("cols"),
                                (r.d(_um(size[0])), r.d(_um(size[1]))) if size else None, h)
        problems = []
        for layer, b in res["needs"]:
            have = nets.window(layer, b) & kdb.Region(b)
            if not (kdb.Region(b) - have).is_empty():
                cut = res["levels"][0]
                problems.append(f"the {cut['cut']} needs {layer} over {_box_out(b, r.dbu)} ({cut['rules']}): "
                                f"{'there is none' if have.is_empty() else 'the ' + layer + ' there is smaller'}; put it on "
                                f"a device's {layer}, or draw the {layer} with its implant first (add_tap draws a tap)")
        shapes, fills, more = _bridged(nets, r, res["shapes"])
        problems += more + _problems(nets, r, shapes, None, a.get("net"))
        if problems:
            raise _refuse("the via", problems, "Move it (x, y), or change rows / cols / size / direction")
        k = r.dbu
        return shapes, {"at": [round(px * k, 4), round(py * k, 4)], "from_layer": frm, "to_layer": to,
                        "direction": "h" if h else "v",
                        "levels": [{"cut": l["cut"], "rows": l["rows"], "cols": l["cols"], "size": l["size"],
                                    "pitch": l["pitch"], "bbox": _box_out(l["box"], k)} for l in res["levels"]],
                        "pads": {n: _box_out(b, k) for n, b in res["pads"].items()},
                        **({"bridged": {n: [_box_out(b, k) for m, b in fills if m == n] for n in dict(fills)}} if fills else {})}
    return _plan_or_draw(c, a, "add_via", plan)


def _endpoint(nets, r, spec, layer: str) -> dict:
    """A route's end: a point [x, y] (um), on the shape there of the route's layer, else of the nearest layer below
    (above) it (a via stack joins it), else free; a device terminal 'XMS:S'; or a net by its label or schematic name
    ('vin'). Its shapes per conductor, and the nets it is."""
    import klayout.db as kdb

    if isinstance(spec, list):
        if len(spec) != 2:
            raise ToolError("a point is [x, y] in um")
        p = (r.snap(r.d(_um(spec[0]))), r.snap(r.d(_um(spec[1]))))
        at = kdb.Region(kdb.Box(p[0] - 1, p[1] - 1, p[0] + 1, p[1] + 1))
        order = [c_.name for c_ in r.st.conductors]
        i = order.index(layer) if layer in order else 0
        for n in [layer, *reversed(order[:i]), *order[i + 1:]]:
            if n in nets.R and (keys := set(nets.touching(n, at))):
                return {"what": f"{_um(spec[0]):g},{_um(spec[1]):g}", "point": p, "on": n, "layers": {}, "keys": keys}
        return {"what": f"{_um(spec[0]):g},{_um(spec[1]):g}", "point": p, "on": layer, "layers": {}, "keys": set()}
    s = str(spec or "").strip()
    if not s:
        raise ToolError("from and to are a point [x, y], a device terminal ('XMS:S', 'XCB:c0') or a net name")
    if ":" in s:
        inst, term = s.rsplit(":", 1)
        dev = nets.devices.get(inst) or nets.piece(inst)          # a schematic instance (all its pieces), or a piece's id
        if dev is None:
            raise ToolError(f"{s}: no layout instance bound to {inst} (bound: {', '.join(sorted(nets.devices)[:30]) or 'none'}); "
                            "or give a piece's id (child@x,y, as get_layout_info lists it)")
        t = next((t for t in dev["terminals"] if t.lower() == term.lower()), None)
        if t is None:
            raise ToolError(f"{s}: {inst} has no terminal {term}" + (f" (it has {', '.join(dev['terminals'])})" if dev["terminals"]
                            else f" Undine recognises ({dev['cell']} holds no single MOSFET, MOSFET in fingers or MIM capacitor)"))
        v = dev["terminals"][t]
        return {"what": f"{inst}:{t}", "layers": {n: g for n, g in v["layers"].items() if n in nets.conductors},
                "keys": set(v.get("keys") or ([v["key"]] if v["key"] is not None else []))}
    keys = nets.find(s)
    if not keys:
        raise ToolError(f"{s}: no net of the layout is called {s} (by a label or the schematic net of a device terminal "
                        "on it); give a point [x, y] or a terminal 'INST:TERM'")
    return {"what": s, "layers": _net_layers(nets, keys), "keys": set(keys), "named": True}


def _metal_pieces(nets, e: dict) -> dict:
    """The pieces a route's end is on through the wiring alone, the n-wells joining nothing (Nets.metal_pieces):
    {piece: its shapes per conductor}."""
    if "point" in e:
        import klayout.db as kdb
        key = nets.metal_at(e["on"], kdb.Point(*e["point"]))
        return {key: {}} if key is not None else {}
    return nets.metal_pieces(e["layers"])


def _merged_layers(parts: list[dict]) -> dict:
    import klayout.db as kdb

    out: dict = {}
    for layers in parts:
        for n, g in layers.items():
            out.setdefault(n, kdb.Region()).insert(g)
    return out


def _net_layers(nets, keys) -> dict:
    """The shapes of some nets per conductor."""
    import klayout.db as kdb

    layers: dict[str, kdb.Region] = {}
    for key in keys:
        for n in nets.conductors:
            g = nets.shapes(key, n)
            if not g.is_empty():
                layers.setdefault(n, kdb.Region()).insert(g)
    return layers


def _anchor(r, end: dict, layer: str, toward: tuple[int, int], width: int) -> tuple[tuple[int, int], str]:
    """Where a route leaves an end, and from which layer: on `layer` when the end has shapes there, else from its
    nearest conductor (a via stack up or down), at the point of those shapes nearest `toward`, kept inside them by half
    the wire (or cut): a shape narrower than that by its centre line."""
    import klayout.db as kdb
    from undine.layouts import drawing

    if "point" in end:
        return end["point"], end["on"]
    order = r.st.order()
    have = [n for n in end["layers"] if n in order and n not in (r.st.diff, r.st.tap)]
    if not have:
        raise ToolError(f"{end['what']} has no shape on a routing layer")
    at = order.index(layer)
    if layer in have:
        on, margin = layer, width // 2
    else:
        below = [n for n in have if order.index(n) < at]
        on = max(below, key=order.index) if below else min(have, key=order.index)
        ch = r.chain(on, layer)
        margin = r.d((ch[0] if below else ch[-1]).size) // 2          # the cut that lands on it
    inner = kdb.Region()
    for poly in end["layers"][on].merged().each():
        one = kdb.Region(poly)
        shrunk = one.sized(-max(0, margin - 1))
        inner.insert(shrunk if not shrunk.is_empty() else one)
    p = drawing.nearest(inner, toward)
    return (r.snap(p[0]), r.snap(p[1])), on


def _turned(nets, on: str, p: tuple[int, int], horizontal: bool) -> str:
    """How a via's turned pads lie (route's vias[].pads): along the shape under them (a strip) or across the way the
    route comes in (on a shape that runs the way of the approach)."""
    import klayout.db as kdb

    at = kdb.Box(p[0] - 1, p[1] - 1, p[0] + 1, p[1] + 1)
    hit = next((q.bbox() for q in (nets.window(on, at) if on in nets.R else kdb.Region()).each()
                if q.bbox().contains(kdb.Point(*p))), None)
    if hit is not None and (hit.width() >= hit.height()) == horizontal:
        return "turned along what it lands on"
    return "turned across the way it comes in"


def route(c: ToolCtx, a: dict):
    """An L / Z wire on one layer between two ends (points, device terminals, nets), with via stacks where an end is on
    another layer; the first way (straight, the two L's, then Z's) that touches no other net and keeps the spacings."""
    import klayout.db as kdb

    layer = a.get("layer")
    if not layer or a.get("from") is None or a.get("to") is None:
        raise ToolError("from, to and layer are required: from / to a point [x, y], a terminal 'XMS:S' or a net name")
    through = a.get("through") or []
    if not isinstance(through, list) or any(not isinstance(p, list) or len(p) != 2 for p in through):
        raise ToolError("through is [[x, y], ...] in um: points the route passes, in order")

    def plan(db, cell, r, nets):
        from undine.layouts import drawing

        cond = r.st.conductor(layer)
        if cond is None:
            raise ToolError(f"layer is a routing layer of the stack: {', '.join(c_.name for c_ in r.st.conductors)}")
        w = r.up(r.d(_um(a["width"]))) if a.get("width") else r.up(r.d(cond.width))
        if w < r.d(cond.width):
            raise ToolError(f"width {w * r.dbu:g} um is below {layer}'s minimum {cond.width:g} um")
        A, B = _endpoint(nets, r, a["from"], layer), _endpoint(nets, r, a["to"], layer)
        net = (a.get("net") or "").strip() or None
        names = [{x.lower(): x for k in e["keys"] for x in nets.names(k)} for e in (A, B)]
        for e, n in zip((A, B), names):
            if net and n and net.lower() not in n:
                raise ToolError(f"{e['what']} is on {' / '.join(n.values())}, not on {net}: route nothing between other nets")
        if not net and names[0] and names[1] and not set(names[0]) & set(names[1]):
            raise ToolError(f"{A['what']} is on {' / '.join(names[0].values())} and {B['what']} on "
                            f"{' / '.join(names[1].values())}: a route between them would short two nets of the schematic")
        net = net or next(iter({**names[1], **names[0]}.values()), None)
        shared = A["keys"] & B["keys"]
        both = bool(shared) and A["keys"] <= shared and B["keys"] <= shared
        well = None                                    # one net only through an n-well: the metal is still to draw
        if both and len(shared) == 1 and not through:
            ma, mb = _metal_pieces(nets, A), _metal_pieces(nets, B)
            common = set(ma) & set(mb)
            if not ma or not mb or (common and set(ma) <= common and set(mb) <= common):
                raise ToolError(f"{A['what']} and {B['what']} are one net already "
                                f"({' / '.join(nets.describe(k_) for k_ in shared)}): nothing to route (through [[x, y], "
                                "...] draws another path between them anyway)")
            well = (f"{A['what']} and {B['what']} were joined only through an n-well (a well is no wire): this draws "
                    "the metal between them")
            for e, m in ((A, ma), (B, mb)):            # an end in metal pieces: from a piece the other end is not on
                if set(m) - common and ("point" not in e):
                    left = _merged_layers([v for k_, v in m.items() if k_ not in common])
                    if left:
                        e["layers"] = left

        def narrow(e, keys):                           # the end's part on some of its nets
            if e.get("named"):
                e["layers"] = _net_layers(nets, keys)
                return
            theirs = _net_layers(nets, keys)           # a terminal: its own shapes on those nets
            left = {n: g & theirs[n] for n, g in e["layers"].items() if n in theirs}
            left = {n: g for n, g in left.items() if not g.is_empty()}
            if left:
                e["layers"] = left
        if both and len(shared) > 1 and "point" not in A and "point" not in B:
            one = min(shared, key=repr)                # both on the same few nets (strips joined pairwise): one to
            narrow(A, {one})                           # the others
            narrow(B, shared - {one})
        elif shared:
            for e in (A, B):                           # a net in pieces, a terminal's strips not joined yet: from
                if e["keys"] - shared and "point" not in e:    # a piece (a strip) the other end is not on
                    narrow(e, e["keys"] - shared)

        def ref(e):
            if "point" in e:
                return e["point"]
            g = e["layers"].get(layer) or next(iter(e["layers"].values()))
            return g.bbox().center().x, g.bbox().center().y
        pts = [(r.snap(r.d(_um(x))), r.snap(r.d(_um(y)))) for x, y in through]
        if pts:                                        # each end where it is nearest its through point
            pa, ona = _anchor(r, A, layer, pts[0], w)
            pb, onb = _anchor(r, B, layer, pts[-1], w)
        else:                                          # where the ends are nearest each other
            pb, onb = _anchor(r, B, layer, ref(A), w)
            pa, ona = _anchor(r, A, layer, pb, w)
            pb, onb = _anchor(r, B, layer, pa, w)
            if pa == pb:
                raise ToolError(f"{A['what']} and {B['what']} meet at {pa[0] * r.dbu:g},{pa[1] * r.dbu:g} already: "
                                "nothing to route")
        if pts:
            way = [pa]
            for q in [*pts, pb]:
                if way[-1][0] != q[0] and way[-1][1] != q[1]:
                    way.append((q[0], way[-1][1]))
                way.append(q)
            ways = [way]
        else:
            ways = drawing.manhattan(pa, pb, grid=r.grid)
        allowed = A["keys"] | B["keys"]
        tried, blocked, rounds = [], [], 0
        clear = r.up(r.d(cond.space) + w // 2 + max((r.d(v) for _, v in cond.wide), default=0))
        i = 0
        while i < len(ways):
            way = [(r.snap(x), r.snap(y)) for x, y in ways[i]]           # every point and bend on the grid (x.1b)
            way = [p for j, p in enumerate(way) if j == 0 or p != way[j - 1]]
            i += 1
            ends = [(way[0], ona, way[1] if len(way) > 1 else way[0]), (way[-1], onb, way[-2] if len(way) > 1 else way[-1])]
            ends = [(p, on, nxt[1] == p[1]) for p, on, nxt in ends if on != layer]
            # each via's pads along the way it is left (the approach), else turned a quarter (along a source / drain
            # strip it comes onto across, next to another strip): the first that fits
            first = None
            for turn in [(False, False), (True, False), (False, True), (True, True)][:2 ** len(ends)]:
                shapes = drawing.segments(r, layer, way, w) if len(way) > 1 else []
                vias = []
                for (p, on, along), t in zip(ends, turn):
                    v = drawing.via_stack(r, p[0], p[1], on, layer, horizontal=along != t)
                    shapes += v["shapes"]
                    vias.append({"at": [round(p[0] * r.dbu, 4), round(p[1] * r.dbu, 4)], "from": on, "to": layer,
                                 "cuts": ", ".join(f"{l['cut']} {l['cols']}x{l['rows']}" for l in v["levels"]),
                                 **({"pads": _turned(nets, on, p, along != t)} if t else {})})
                if not shapes:
                    raise ToolError(f"{A['what']} and {B['what']} meet already on {layer}: nothing to route")
                shapes, _fills, problems = _bridged(nets, r, shapes)       # a poly contact's npc next to another's
                problems += _problems(nets, r, shapes, allowed, net, blocked if first is None else None)
                if not problems:
                    k = r.dbu
                    return shapes, {"net": net, "from": A["what"], "to": B["what"], "layer": layer, "width": round(w * k, 4),
                                    "points": [[round(x * k, 4), round(y * k, 4)] for x, y in way], "vias": vias,
                                    "length": round(sum(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p, q in zip(way, way[1:])) * k, 4),
                                    **({"tried": i} if i > 1 else {}), **({"note": well} if well else {})}
                first = first or problems
            tried.append((way, first))
            if i == len(ways) and not pts and blocked and rounds < 3:     # round the box of what was in the way
                rounds += 1
                box = kdb.Box()
                for b in blocked:
                    box = box + b
                ways += [x for x in drawing.detours(pa, pb, box, clear, r.grid) if x not in ways]
        k = r.dbu
        raise ToolError(f"no route from {A['what']} to {B['what']} on {layer} is clear (tried {len(tried)}): " + " | ".join(
            f"via {[[round(x * k, 3), round(y * k, 3)] for x, y in way[1:-1]] or 'straight'}: " + "; ".join(p[:3])
            for way, p in tried[:4]) + ". Pass through: [[x, y], ...] to lead it round, another layer, or a narrower width")
    return _plan_or_draw(c, a, "route", plan)


def _tap_checks(nets, r, shapes: list, kind: str) -> list[str]:
    """The front-end rules of a tap or ring: not against another diffusion (it is not a butted tap), the tap spacing
    to diffusion and taps and to poly; its implant not over the other implant, and apart from (or merged with) the
    implant of its kind; a substrate tap out of the n-well by its spacing; an n-well tap's n-well apart from (or
    merged with) another n-well and away from n+ diffusion."""
    import klayout.db as kdb

    t, st, k = r.st.taps, r.st, r.dbu
    tap = kdb.Region([b for n, b in shapes if n == st.tap]).merged()
    out = []
    for p in list(nets.window(st.diff + "$raw", tap.bbox().enlarged(1, 1)).interacting(tap).each())[:2]:
        out.append(f"the tap touches a diffusion at {_box_out(p.bbox(), k)}")
    for lay in (st.diff + "$raw", st.tap):
        for p in nets.near(lay, tap, r.d(t.space))[:2]:
            out.append(f"the tap comes closer than {t.space:g} um to {lay.removesuffix('$raw')} at {_box_out(p.bbox(), k)} "
                       "(diff/tap.3)")
    sp = dict(r.st.conductor(st.poly).away).get(st.tap) if r.st.conductor(st.poly) else None
    if sp:                                             # poly over or next to it (poly.5)
        near = nets.window(st.poly, tap.bbox().enlarged(r.d(sp), r.d(sp))).interacting(tap.sized(r.d(sp) - 1))
        for p in list(near.each())[:2]:
            out.append(f"the tap comes closer than {sp:g} um to poly at {_box_out(p.bbox(), k)}")
    mine, other = t.implant[kind], t.implant["ntap" if kind == "ptap" else "ptap"]
    imp = kdb.Region([b for n, b in shapes if n == mine]).merged()
    for p in list((nets.window(other, imp.bbox()) & imp).merged().each())[:2]:
        out.append(f"its {mine} overlaps {other} at {_box_out(p.bbox(), k)}: give it room, or a smaller box")
    for p in nets.near(mine, imp, r.d(t.implant_space))[:2]:
        out.append(f"its {mine} comes closer than {t.implant_space:g} um to {mine} at {_box_out(p.bbox(), k)} ({mine}.1): "
                   "move it that far, or onto that implant")
    if kind == "ptap":
        for p in list(nets.window(st.well, tap.bbox().enlarged(r.d(t.well_space), r.d(t.well_space))).interacting(
                tap.sized(r.d(t.well_space) - 1)).each())[:2]:
            out.append(f"the substrate tap is within {t.well_space:g} um of the n-well at {_box_out(p.bbox(), k)} (diff/tap.11)")
    well = kdb.Region([b for n, b in shapes if n == st.well]).merged()
    if not well.is_empty():
        for p in nets.near(st.well, well, r.d(t.well_well))[:2]:
            out.append(f"its n-well comes closer than {t.well_well:g} um to an n-well at {_box_out(p.bbox(), k)} (nwell.2a): "
                       "overlap it (the same net) or keep that far")
        reach = r.d(t.well_ndiff)
        ndiff = nets.window(st.diff + "$raw", well.bbox().enlarged(reach, reach)) & \
            nets.window(t.implant["ntap"], well.bbox().enlarged(reach, reach))
        for p in list(ndiff.merged().interacting(well.sized(reach - 1)).each())[:2]:
            out.append(f"its n-well comes closer than {t.well_ndiff:g} um to an n+ diffusion at {_box_out(p.bbox(), k)} "
                       "(diff/tap.9)")
    return out


def _tap_kind(a: dict) -> str:
    kind = (a.get("kind") or "").lower()
    if kind not in ("ptap", "ntap"):
        raise ToolError("kind is ptap (a substrate tap, usually vss) or ntap (an n-well tap, usually vdd)")
    return kind


def _taps(r, op: str):
    """The stack's tap rules, or why there are none: the PDK draws its taps as diffusion with an implant."""
    if r.st.taps is None or not r.st.tap:
        raise ToolError(f"{op}: {r.profile.label} has no tap rules in Undine yet (its taps are {r.st.diff} with an "
                        "implant): draw the tap with add_box (and its implant and well) and add_via / route to it, then "
                        "check with run_checks")
    return r.st.taps


def add_tap(c: ToolCtx, a: dict):
    """A rule-correct substrate or n-well tap: a box (um) or the smallest tap at x, y, with its implant (and n-well),
    contacts along it and the first metal (and met1 with metal met1) over them."""
    import klayout.db as kdb

    kind = _tap_kind(a)
    if a.get("box") is None and (a.get("x") is None or a.get("y") is None):
        raise ToolError("give box [x1, y1, x2, y2] (the tap) or x, y (the smallest tap there)")
    box = _box_um(a) if a.get("box") is not None else None

    def plan(db, cell, r, nets):
        from undine.layouts import drawing

        _taps(r, "add_tap")
        lic = next(x for x in r.st.cuts if x.below == r.st.tap)
        if box:
            b = kdb.Box(*(r.snap(r.d(v)) for v in box))
        else:                                          # one contact, the larger enclosure along x
            w, h = r.up(r.d(lic.size + 2 * lic.enc_below[1])), r.up(r.d(lic.size + 2 * lic.enc_below[0]))
            x1 = r.snap(r.d(_um(a["x"]))) - w // 2 // r.grid * r.grid
            y1 = r.snap(r.d(_um(a["y"]))) - h // 2 // r.grid * r.grid
            b = kdb.Box(x1, y1, x1 + w, y1 + h)
        shapes = drawing.tap(r, b, kind, a.get("metal") or "met1")
        problems = _tap_checks(nets, r, shapes, kind) + _problems(nets, r, shapes, None, a.get("net"))
        if problems:
            raise _refuse("the tap", problems, "Move it, or make it smaller")
        return shapes, {"kind": kind, "tap": _box_out(b, r.dbu), "net": a.get("net"),
                        "contacts": sum(1 for n, _ in shapes if n == lic.name)}
    out = _plan_or_draw(c, a, "add_tap", plan)
    out["note"] = ("route it to its net (route from the tap's met1, or add_label the net there); LVS joins it to the "
                   "substrate / n-well it sits in")
    return out


def guard_ring(c: ToolCtx, a: dict):
    """A closed tap ring around instances (their extent) or a box, a spacing out: tap, implant, contacts and the metal
    strap ring; an ntap ring's n-well covers what it encloses."""
    import klayout.db as kdb

    kind = _tap_kind(a)
    around = a.get("around")
    if not around:
        raise ToolError("around is the instances to enclose (['XM1', 'XM2']) or a box [x1, y1, x2, y2] in um")
    is_box = isinstance(around, list) and len(around) == 4 and all(isinstance(v, (int, float)) for v in around)

    def plan(db, cell, r, nets):
        from undine.layouts import drawing

        t = _taps(r, "guard_ring")
        if is_box:
            inner = kdb.Box(*(r.snap(r.d(_um(v))) for v in around))
            enclosed = [n for n, i in ((i["name"], i) for i in db.named_instances(cell))
                        if inner.contains(kdb.Box(*i["bbox"]).center())]
        else:
            try:                                       # every piece bound to a name (place_pcell's, sync_layout's), or an id
                placed = _find_instances(db, cell, [str(n) for n in around])
            except ValueError as e:
                raise ToolError(f"around: {e}") from None
            inner = kdb.Box()
            for i in placed:
                inner = inner + kdb.Box(*i["bbox"])
            enclosed = list(dict.fromkeys(i.get("name") or i["id"] for i in placed))
            # default: the tap spacing, and the ring's implant the implant spacing from the devices' (at their extent)
            sp = r.up(r.d(_um(a["spacing"]))) if a.get("spacing") is not None else \
                r.up(r.d(max(t.space, t.implant_space + t.implant_enc)))
            inner = kdb.Box(r.snap(inner.left - sp), r.snap(inner.bottom - sp), r.snap(inner.right + sp), r.snap(inner.top + sp))
        if kind == "ntap" and (n_ := [n for n in enclosed if (nets.devices.get(n) or {}).get("class") == "nmos"]):
            raise ToolError(f"an ntap ring's n-well covers what it encloses, and {', '.join(n_)} is an nMOS: use a ptap "
                            "ring, or enclose only pMOS devices")
        w = r.up(r.d(_um(a["width"]))) if a.get("width") else None
        shapes = drawing.guard_ring(r, inner, kind, w, a.get("metal") or "met1")
        problems = _tap_checks(nets, r, shapes, kind) + _problems(nets, r, shapes, None, a.get("net"))
        if problems:
            raise _refuse("the guard ring", problems, "Give it more spacing, or a box clear of other shapes")
        tap = kdb.Region([b for n, b in shapes if n == r.st.tap]).bbox()
        return shapes, {"kind": kind, "net": a.get("net"), "inner": _box_out(inner, r.dbu), "outer": _box_out(tap, r.dbu),
                        "encloses": enclosed}
    out = _plan_or_draw(c, a, "guard_ring", plan)
    out["note"] = "route the ring to its net (route from its metal, or add_label the net on it); routes leave the ring on a layer above its metal"
    return out


def sync_layout(c: ToolCtx, a: dict):
    from undine.layouts import sdl as sdlmod

    def fn(db, cell):
        prj = c.srv.project(c.work)
        sch = prj.library(a["lib"]).path / f"{cell}.sch"
        if not sch.is_file():
            raise KeyError(f"no schematic {a['lib']}/{cell}" + ("" if db.layout.cell(cell) is not None else
                                                              f": {cell} has neither a schematic nor a layout"))
        made = db.layout.cell(cell) is None
        res = sdlmod.Sdl(prj.pdk, db, sch, xschem.SymbolResolver(prj.symbol_paths())).sync(a.get("instances") or None,
                                                                                         a.get("layout") or None)
        if made:
            res["created"] = f"the layout view of {a['lib']}/{cell}"
        return res
    res = _lwrite(c, a, "sdl_sync", fn, need=False)
    try:
        _w, _l, cell, db = _ldb(c, a, write=False)
        bad = []
        for i in db.named_instances(cell):
            bad += [{"instance": i["name"], "problem": o["kind"], "where_um": [round(v * db.layout.dbu, 3) for v in o["where"]]}
                    for o in db.foreign_overlaps(cell, i["name"])]
        if bad:
            res["warnings"] = (res.get("warnings") or []) + bad
            res["warning_note"] = ("shapes in the cell now overlap device transistor areas (e.g. a poly bar joining gates reaches into the "
                                   "taller diffusion): LVS will extract a wrong L / W until they are fixed (see check_device_integrity)")
    except Exception:
        pass
    res["note"] = ("Updated devices keep their position, but routing is not redone: existing wires may no longer meet the new "
                   "device shapes. Check with get_device_geometry and run_checks, and fix connections with add_box / add_path.")
    return res


# ---------------------------------------------------------------------------------- layout assembly: libraries, cells, imports


def create_library(c: ToolCtx, a: dict):
    """A new library of the project in the working copy (project.json, libs/<name>), recorded in its own log."""
    with c.srv.changeset_lock:
        return _create_library(c, a)


def _create_library(c: ToolCtx, a: dict):
    from undine.store.project import check_name

    work = c.need_changeset()
    _check_limit(c)
    if not (a.get("reason") or "").strip():
        raise ToolError("reason is required: say why this change is made")
    try:
        name = check_name((a.get("name") or "").strip())       # as POST /libs
    except ValueError as e:
        raise ToolError(f"{e}: a library name is letters, digits, _ . + - and starts with a letter, digit or _") from e
    prj = c.srv.project(work)
    if (have := prj.libraries().get(name)) is not None:          # a PDK's or a dependency's too: the name would hide it
        raise ToolError(f"library {name} exists" + (f" ({have.origin}, read-only)" if have.readonly else "") +
                        ": use it, or choose another name")
    log = c.srv.oplog(work, name)
    log.guard()
    try:
        prj.create_library(name)
    except (ValueError, PermissionError) as e:
        raise ToolError(str(e)) from e
    log.append({"op": "create_library", "args": {"name": name}, "actor": c.actor, "reason": a["reason"],    # restore takes it out
                "changeset": c.changeset, "cell": None, "view": None, "sha": None, "effects": [{"k": "l", "do": "add", "lib": name}]})
    c.writes += 1
    return {"ok": True, "library": name,
            "note": "the library is in your working copy only; create its cells with create_cell (lib: this library)"}


ROTATIONS = (0, 90, 180, 270)
ARRAY_MAX = 1 << 20      # elements of one array (columns x rows): a CDAC has thousands; a million is a unit slip that DRC / LVS would never finish


def _whole(a: dict, key: str) -> int:
    try:
        v = float(1 if a.get(key) is None else a[key])
    except (TypeError, ValueError):
        v = 0.5
    if v != int(v) or not 1 <= v <= ARRAY_MAX:
        raise ToolError(f"{key} must be a whole number from 1 to {ARRAY_MAX}")
    return int(v)


def _bring_child(c: ToolCtx, db, lib: str, child: str, child_lib: str | None) -> dict:
    """Makes `child` a layout cell of `lib`: it is one already, or it is copied in, with the cells under it (place_from),
    from `child_lib` or from the PDK's standard cells (their GDS in the PDK). Says where it comes from ("from"), and
    which cells under it were copied under another name ("renamed": lib has cells of those names with other content)."""
    from undine.layouts import layout

    have = db.layout.cell(child)
    have = have if have is not None and not have.is_ghost_cell() else None
    if child_lib and child_lib != lib:
        hit = layout.stdcell_layout(db.pdk, child)
        std = hit is not None and hit[0] == child_lib
        src, where = (hit[1], f"the PDK's {child_lib}") if std else (c.srv.db(c.work, child_lib).layout, child_lib)
        sc = src.cell(child)
        if sc is None or sc.is_ghost_cell():
            raise KeyError(f"no layout cell {child} in {child_lib}")
        if have is not None:                             # the whole tree: a cell under it may differ, not only its own shapes
            if layout.tree_content(db.layout, have) != layout.tree_content(src, sc):
                raise ValueError(f"{lib} has a cell {child} of its own, other than {where}'s (it, or a cell under it, "
                                 f"differs): place {lib}'s (leave out child_lib), or give one of them another name")
            return {"from": f"{lib} (as in {where})"}
        if std:
            db.add_stdcell(child)
            return {"from": where}
        renamed = db.copy_cell_from(src, child, child_lib)
        return {"from": where, **({"renamed": renamed} if renamed else {})}
    if have is not None:
        return {"from": lib}
    if (std := db.add_stdcell(child)):
        return {"from": f"the PDK's {std}"}
    others = [n for n, l in c.srv.project(c.work).libraries().items()
              if n != lib and l.origin == "project" and l.has_layout() and child in l.layout_cells()[0]]
    raise KeyError(f"no layout cell {child} in {lib}" + (f"; {', '.join(others)} has one: pass child_lib" if others else
                   "; create it (create_cell with views [layout]), import it (import_layout), or name a PDK standard cell"))


def _binding_note(c: ToolCtx, lib: str, cell: str, name: str) -> str | None:
    """What schematic-driven layout makes of the instance just bound to schematic instance `name` (Sdl.binding), when
    it is not simply that device: get_layout_info's list it is in, and whether sync_layout replaces it."""
    from undine.layouts import sdl as sdlmod

    prj = c.srv.project(c.work)
    sch = prj.library(lib).path / f"{cell}.sch"
    if not sch.is_file():
        return None
    try:
        b = sdlmod.Sdl(prj.pdk, c.srv.db(c.work, lib), sch, xschem.SymbolResolver(prj.symbol_paths())).binding(name)
    except Exception:
        return None
    if b is None:
        return f"the schematic {lib}/{cell} has no instance {name}: get_layout_info lists it as extra until it has"
    if b["own_layout"]:
        return (f"{b['own_layout']}: sync_layout leaves it as it is, also when {name}'s parameters change (place it "
                f"again then); get_layout_info lists it under own_layout" +
                (f" (the schematic's {name} would be a {b['expected']})" if b["expected"] else ""))
    have, units = b["units"]
    if b["out_of_date"] and have != units:
        return (f"{have} units placed, and the schematic's {name} has {units} (MF x m): get_layout_info lists it as out "
                f"of date, and sync_layout puts {units} of {b['expected']} in its place")
    if b["out_of_date"]:
        return (f"the schematic's {name} is a {b['expected']}, not this cell: get_layout_info lists it as out of date, and "
                f"sync_layout puts a {b['expected']} in its place")
    return None


def place_cell(c: ToolCtx, a: dict):
    """A layout cell placed in a cell's layout: of the library, of another library of the project (copied in), or a PDK
    standard cell; with columns / rows one regular array instance (an array is one device binding, see the docs)."""
    child = (a.get("child") or "").strip()
    if not child:
        raise ToolError("child is required: the layout cell to place (a cell of lib or of child_lib, or a PDK standard "
                        "cell such as sky130_fd_sc_hd__inv_1)")
    if a.get("x") is None or a.get("y") is None:
        raise ToolError("x and y are required: where the child's origin goes, in um")
    x, y = _um(a["x"]), _um(a["y"])
    try:
        rot = int(float(a.get("rot") or 0)) % 360
    except (TypeError, ValueError) as e:
        raise ToolError("rot is 0, 90, 180 or 270 (degrees, counter-clockwise)") from e
    if rot not in ROTATIONS:
        raise ToolError("rot is 0, 90, 180 or 270 (degrees, counter-clockwise)")
    cols, rows = _whole(a, "columns"), _whole(a, "rows")
    if cols * rows > ARRAY_MAX:
        raise ToolError(f"columns x rows is {cols * rows}, at most {ARRAY_MAX} elements in one array: check the counts (a unit "
                        "slip?); DRC and LVS of a larger array would not finish")
    px, py = _um(a.get("pitch_x") or 0), _um(a.get("pitch_y") or 0)
    if (cols > 1 and not px) or (rows > 1 and not py):
        raise ToolError("an array needs pitch_x (with columns > 1) and pitch_y (with rows > 1): the step between "
                        "neighbouring copies in um")
    name = (a.get("name") or "").strip() or None

    def fn(db, cell):
        k = db.layout.dbu
        if name and any(i["name"] == name for i in db.named_instances(cell)):
            raise ValueError(f"{name} is placed in {cell} already: move it with move_instance, or bind another name")
        came = _bring_child(c, db, a["lib"], child, a.get("child_lib") or None)
        placed = db.place(cell, child, x / k, y / k, rot // 90, bool(a.get("mirror")), name, cols, rows, (px / k, py / k))
        out = {"ok": True, "cell": child, **came, "instance": name,
               "bbox_um": [round(v * k, 3) for v in placed[0]["bbox"]] if placed else None}
        if cols * rows > 1:
            out["array"] = {"columns": cols, "rows": rows, "pitch_um": [px, py]}
        return out
    res = _lwrite(c, a, "place_cell", fn)
    notes = []
    if res.get("renamed"):
        notes.append(f"the copy of {child} places " + ", ".join(f"{here} (not {there}: {a['lib']} has a {there} with other content)"
                                                                for there, here in res["renamed"].items()))
    if name and (why := _binding_note(c, a["lib"], a["cell"], name)):
        notes.append(why)
    if notes:
        res["warning"] = "; ".join(notes)
    if not name:
        res["note"] = "not bound to a schematic instance (no name): LVS sees its devices, get_layout_info counts it as unbound"
    return res


IMPORT_SUFFIXES = (".gds", ".oas", ".oasis")


def _import_source(c: ToolCtx, a: dict) -> tuple["Path", dict]:
    """The file to import and where it comes from: a GDS / OASIS file under the server's import roots (as POST
    /l/{lib}/import), or the final GDS of a LibreLane run (as POST /jobs/{job}/import-gds)."""
    from pathlib import Path

    if bool(a.get("path")) == bool(a.get("job")):
        raise ToolError("give path (a GDS / OASIS file a generator wrote) or job (a run_librelane job), one of them")
    if a.get("path"):
        src = Path(a["path"]).expanduser().resolve()
        if not any(src.is_relative_to(r) for r in c.srv.import_roots):
            raise ToolError(f"{src} is outside the directories this server imports from ({', '.join(map(str, c.srv.import_roots))}; "
                            "--import-root): write the file there")
        if src.suffix.lower() not in IMPORT_SUFFIXES:
            raise ToolError(f"{src.name}: import a {' / '.join(IMPORT_SUFFIXES)} file")
        if not src.is_file():
            raise ToolError(f"no file {src}")
        return src, {"file": str(src)}
    for proj in dict.fromkeys([c.work, c.project]):                 # a run of the working copy first
        try:
            job = c.srv.jobs.get(c.srv.project(proj).path, a["job"])
        except KeyError:
            continue
        st = job.status()
        if st.get("kind") != "librelane":
            raise ToolError(f"job {a['job']} is not a LibreLane run")
        if st.get("state") != "done" or not (gds := librelane.results(job.path).get("gds")):
            raise ToolError(f"LibreLane run {a['job']} has no final GDS ({st.get('state')}); see get_librelane_result")
        return job.path / gds, {"job": a["job"], "file": gds}
    raise ToolError(f"no job {a['job']}")


def import_layout(c: ToolCtx, a: dict):
    """The cells of a GDS / OASIS file (a generator's, or a LibreLane run's final GDS) into a library's layout in the
    changeset, recorded (LayoutDB.import_cells): never over a cell with other content unless it is named in replace."""
    with c.srv.changeset_lock:
        return _import_layout(c, a)


def _import_layout(c: ToolCtx, a: dict):
    from undine.layouts import layout

    work = c.need_changeset()
    _check_limit(c)
    lib = a.get("lib")
    if not lib:
        raise ToolError("lib is required: the library the cells go into")
    if not (a.get("reason") or "").strip():
        raise ToolError("reason is required: say why this change is made")
    replace = a.get("replace") or []
    if replace is not True and not (isinstance(replace, list) and all(isinstance(n, str) for n in replace)):
        raise ToolError("replace is a list of cell names (or true: every cell the file has with other content)")
    path, src = _import_source(c, a)
    try:
        library = c.srv.project(work).library(lib)
    except KeyError as e:
        raise ToolError(f"no library {lib}: create it with create_library") from e
    if library.readonly or library.layout_file:
        raise ToolError(f"library {lib} is read-only (a dependency or the PDK); import into a project library")
    db = c.srv.db(work, lib)
    args = {k: a[k] for k in ("path", "job", "replace") if a.get(k)}
    try:
        with db.recording("import_layout", args, c.actor, a["reason"], c.changeset):
            res = db.import_cells(path, src, replace)
    except layout.CellClash as e:
        raise ToolError(f"{e}: nothing was imported. To replace them with the file's, call again with replace: "
                        f"{json.dumps(e.cells)} (the review shows each replaced cell's shapes and instances). To keep "
                        f"both, import into a library of its own (create_library) and place from there with "
                        f"place_cell(child_lib=...), or let the generator name its cells differently") from e
    except (KeyError, ValueError, PermissionError, RuntimeError) as e:
        raise ToolError(str(e).strip("'\"")) from e
    c.writes += 1
    out = {"ok": True, **res}
    if not res["added"] and not res["replaced"] and not res["stdcells"]:
        out["note"] = f"{lib} has every cell of the file already, the same: nothing changed"
    else:
        out["note"] = ("place the file's top cell with place_cell, or open it as a cell of its own (get_layout_info); "
                       "run_checks checks the cells it changed")
    return out


# ---------------------------------------------------------------------------------- checks and simulation


def run_checks(c: ToolCtx, a: dict):
    c.need_changeset(check=True)
    if stopping(c.srv):                            # a check started now would end with the server (sim.Jobs.close)
        return {"checks": [], "stopping": True, "note": CHECKS_NOT_STARTED.format(again="run_checks")}
    try:
        # a reader commits nothing of another's changeset: its writer's next checks (or its lease's end) do
        c.cs.verify(c.project, c.changeset, fresh=bool(a.get("fresh")), commit=not c.reading)
    except ChangesetError as e:
        raise ToolError(str(e)) from e
    t0 = time.time()
    while True:
        rows = c.cs.verify_status(c.project, c.cs._load(c.project, c.changeset))
        # every row has ended (done, failed, cancelled, interrupted ...) or was not run
        if not any(r.get("state") in ("queued", "running") for r in rows) \
                or time.time() - t0 >= WAIT_S or stopping(c.srv):
            break
        time.sleep(1.5)
    out = []
    for r in rows:
        row = {k: r[k] for k in ("side", "kind", "checker", "lib", "cell", "state", "ok", "detail", "error", "job",
                                 "finished", "reused") if r.get(k) is not None}
        if r.get("state") in ("cancelled", "interrupted"):
            row["next"] = "run_checks runs it again"
        if r.get("findings"):                     # an ERC's: the first ones, and how many more
            row["findings"] = r["findings"][:FINDINGS]
            if (more := len(r["findings"]) - FINDINGS + (r.get("findings_more") or 0)) > 0:
                row["findings_more"] = more
        if r.get("fresh") is False:
            row.update(outdated=True, changed=r.get("changed") or [])
        out.append(row)
    changed = {(lc["lib"], op["cell"]) for lc in c.cs.get(c.project, c.changeset)["changes"] for op in lc["ops"]
               if op.get("cell") and op.get("view") in ("schematic", "symbol", "netlist")}
    for lib, cell in sorted(changed):              # the schematic itself: its symbol, supply parameters, pending connections
        if (c.srv.project(c.work).library(lib).path / f"{cell}.sch").is_file():
            found = _schematic_findings(c, lib, cell)
            row = {"side": "work", "kind": "schematic", "lib": lib, "cell": cell, "state": "done", "ok": not found,
                   "detail": "; ".join(found) or
                   "symbol matches the ports; every supply parameter names a net; all drawn"}
            base = _schematic_base(c, lib, cell) if found else None
            if base is not None:                   # a failing row says what the baseline had: the changeset's doing?
                out.append({"side": "base", "kind": "schematic", "lib": lib, "cell": cell, "state": "done",
                            "ok": not base, "detail": "; ".join(base) or "no findings"})
                new = [f for f in found if f not in base]
                row.update({"unchanged_from_baseline": True} if not new else
                           {"new_in_work": new} if len(new) < len(found) else {})
            out.append(row)
    skipped = {}
    try:
        if (unused := c.cs.unused_generated(c.project, c.cs._load(c.project, c.changeset))):
            skipped = {"skipped_unused_cells": {
                "count": len(unused), "cells": [f"{lib}/{cell}" for lib, cell in unused[:10]],
                "why": "PCell variants Undine generated (or sync_layout's cells round them) that nothing places any more: "
                       "not checked, since alone they are no design (Magic's LU.2 would fail them); delete_unused_cells "
                       f"takes them out of the changeset (lib: {', '.join(sorted({lib for lib, _ in unused}))})"}}
    except Exception:
        pass
    if stopping(c.srv) and any(r.get("state") in ("queued", "running", "interrupted") for r in rows):
        return {"checks": out, **skipped, "stopping": True, "note": CHECKS_STOPPING.format(again="run_checks")}
    return {"checks": out, **skipped, "note": "side=base is the design before the changeset, side=work is with the changes. LVS runs when a "
                                   "cell has both schematic and layout; DRC only when the layout itself was changed (sync_layout, "
                                   "add_box, add_path, add_label, route, add_via, add_tap, guard_ring, move_instance, "
                                   "delete_instances, delete_shapes, place_cell, place_pcell, place_array, import_layout), so a "
                                   "schematic-only change gets LVS but no new DRC. checker names the tool: DRC runs the PDK's "
                                   "KLayout deck and, on sky130, GF180 and IHP, Magic's drc(full) too (rules the deck lacks: "
                                   "MV diff / tap, MIM spacing on sky130); get_drc_report shows where each violation is. kind=erc rows are warnings "
                                   "from the LVS connectivity (a body tap butted against a source / drain of another net), not "
                                   "violations. reused: true is a job that had already run on the same inputs (finished: when); "
                                   "fresh: true runs every check anew. kind=schematic rows check each schematic the "
                                   "changeset changed: its symbol against its ports, standard-cell supply parameters, pending "
                                   "connections; a failing one comes with the baseline's row, and says "
                                   "unchanged_from_baseline when the baseline has the same findings (new_in_work: the "
                                   "ones it does not have)."}


# ---------------------------------------------------------------------------------- design intent (intent.py)


def _intent_on(c: ToolCtx, a: dict, names: list[str]) -> list[str]:
    """The intent items whose rule names one of these instances (or nets), as text."""
    lib, cell = _lib_cell(a)
    try:
        model = c.srv.intent_editor(c.work, lib, cell).model()
    except Exception:
        return []
    out = []
    for view, it in intent.all_items(model):
        rule = it.get("rule") or {}
        if set(names) & set(rule.get("instances") or []) or rule.get("net") in names:
            out.append(f"{'[' + view + '] ' if view else ''}{it['id']} {it['kind']}{' [' + it.get('strength', 'must') + ']' if it['kind'] == 'constraint' else ''}: "
                       f"{it.get('text', '')}" + (f" (why: {it['reason']})" if it.get("reason") else ""))
    return out


def get_intent(c: ToolCtx, a: dict):
    lib, cell = _lib_cell(a)
    try:
        v = c.srv.intent_view(c.work, lib, cell)
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    ids, kinds = a.get("ids"), a.get("kinds")
    if (bad := [k for k in kinds or [] if k not in intent.KINDS]):
        raise ToolError(f"kinds: no kind {', '.join(map(str, bad))}; they are {', '.join(intent.KINDS)}")
    if not ids and not kinds:
        return {"intent": v["intent"], "problems": v["problems"],
                "missing": {intent.STEPS[k]: [m["message"] for m in ms] for k, ms in v["requirements"].items() if ms}}
    # only the items asked for (cell and view intent alike), and the problems and missing entries about them
    match = lambda it: (not ids or it.get("id") in ids) and (not kinds or it.get("kind") in kinds)   # noqa: E731
    m = v["intent"]
    items = [it for it in m.get("items") or [] if match(it)]
    views = {name: {**sec, "items": kept} for name, sec in (m.get("views") or {}).items()
             if (kept := [it for it in sec.get("items") or [] if match(it)])}
    found = {it["id"] for it in items} | {it["id"] for sec in views.values() for it in sec["items"]}
    about = lambda e: e.split(".")[-1] in found                                                       # noqa: E731
    out = {"intent": {"items": items, **({"views": views} if views else {})},
           "problems": [p for p in v["problems"] if about(p["entry"])],
           "missing": {intent.STEPS[k]: [x["message"] for x in ms if about(x["entry"])]
                       for k, ms in v["requirements"].items() if any(about(x["entry"]) for x in ms)},
           "note": "only the items asked for; without ids / kinds: the whole intent with notes, ports and supplies"}
    if ids and (unknown := [i for i in ids if i not in {it.get("id") for _, it in intent.all_items(m)}]):
        out["not_found"] = unknown
    return out


def _intent_write(c: ToolCtx, a: dict, op: str, fn):
    lib, cell = _lib_cell(a)
    with c.srv.changeset_lock:
        work = c.need_changeset()
        _check_limit(c)
        if not (a.get("reason") or "").strip():
            raise ToolError("reason is required: say why the intent changes")
        ed = c.srv.intent_editor(work, lib, cell)
        args = {k: v for k, v in a.items() if k not in ("reason", "lib", "cell")}
        try:
            with ed.recording(op, args, c.actor, a["reason"], c.changeset):
                res = fn(ed)
        except (KeyError, ValueError) as e:
            raise ToolError(str(e).strip("'\"")) from e
        c.writes += 1
        return res


def add_intent(c: ToolCtx, a: dict):
    item = {k: a.get(k) for k in ("kind", "text", "strength", "rule", "spec", "serves")}
    item["reason"] = a.get("why")
    if a.get("request"):
        item["source"] = {"request": a["request"], **({"turn": c.turn} if c.turn else {})}
    new_id = _intent_write(c, a, "add_intent", lambda ed: ed.add_item(item, c.actor, a.get("view") or None))
    return {"ok": True, "id": new_id, "note": "say in your reply what you added to the design intent"}


def update_intent(c: ToolCtx, a: dict):
    if not a.get("id") or not isinstance(a.get("fields"), dict):
        raise ToolError("id and fields are required, e.g. {\"id\": \"s1\", \"fields\": {\"spec\": {...}}}")
    fields = dict(a["fields"])
    if "why" in fields:
        fields["reason"] = fields.pop("why")
    added = _intent_write(c, a, "update_intent", lambda ed: ed.update_item(a["id"], fields, c.actor, c.turn))
    if added:
        return {"ok": True, "id": a["id"], "note": "the request was added to the item's request history "
                                                   "(source.requests); say in your reply what the user confirmed"}
    if fields.get("request") is not None:
        return {"ok": True, "id": a["id"], "note": "nothing was added to the request history: the words were empty, or "
                                                   "the same as the item's last ones"}
    return {"ok": True, "id": a["id"]}


def remove_intent(c: ToolCtx, a: dict):
    if not a.get("id"):
        raise ToolError("id is required")
    _intent_write(c, a, "remove_intent", lambda ed: ed.remove_item(a["id"]))
    return {"ok": True, "removed": a["id"]}


def set_port_intent(c: ToolCtx, a: dict):
    if not a.get("port"):
        raise ToolError("port is required")
    fields = {k: a[k] for k in ("kind", "side") if k in a}
    if a.get("why"):
        fields["reason"] = a["why"]
    if a.get("side") == "auto":
        fields["decided_by"] = c.actor
    _intent_write(c, a, "set_port_intent", lambda ed: ed.set_port(a["port"], fields))
    return {"ok": True, "port": a["port"], **fields}


def set_supply_intent(c: ToolCtx, a: dict):
    if not a.get("net"):
        raise ToolError("net is required")
    _intent_write(c, a, "set_supply_intent", lambda ed: ed.set_supply(a["net"], a.get("role")))
    return {"ok": True, "net": a["net"], "role": a.get("role")}


def set_intent_notes(c: ToolCtx, a: dict):
    _intent_write(c, a, "set_intent_notes", lambda ed: ed.set_notes(a.get("text") or ""))
    return {"ok": True}


def extract_parasitics(c: ToolCtx, a: dict):
    """Extracts a layout cell with its parasitics (parasitics.py) and returns the summary to diagnose with."""
    lib, cell = _lib_cell(a)
    mode = a.get("mode") or "c"
    if stopping(c.srv):                            # it would end with the server (sim.Jobs.close)
        return {"stopping": True, "note": CHECKS_NOT_STARTED.format(again="extract_parasitics")}
    try:
        job = c.srv.start_extraction(c.work, lib, cell, mode, a.get("cthresh_ff"),
                                     {"reason": a["reason"], "actor": c.actor} if a.get("reason") else {"actor": c.actor},
                                     a.get("res_threshold_ohm"), a.get("res_nets"))
    except (ValueError, KeyError, RuntimeError) as e:
        raise ToolError(str(e).strip("'\"")) from e
    t0 = time.time()
    while time.time() - t0 < WAIT_S and job.status()["state"] in ("queued", "running") and not stopping(c.srv):
        time.sleep(0.5)
    st = job.status()
    if st["state"] in ("queued", "running", "interrupted") and stopping(c.srv):
        return {"job": job.id, "state": st["state"], "stopping": True, "note": CHECKS_STOPPING.format(again="extract_parasitics")}
    if st["state"] in ("queued", "running"):
        return {"job": job.id, "state": st["state"],
                "note": f"still extracting: wait_for job:{job.id}{_also(c, ' ')}, then get_parasitics{_also(c, ' ')}"}
    if st["state"] != "done":
        raise ToolError(f"extraction {st['state']} (job {job.id}): {st.get('error', '')}\n{job.log(15)}")
    return _parasitics(c, st, None)


def _parasitics(c: ToolCtx, st: dict, net: str | None) -> dict:
    p = c.srv.project(c.work).path / "runs" / st["id"] / "parasitics.json"
    full = json.loads(p.read_text()) if p.is_file() else st.get("summary") or {}
    try:
        out = {"job": st["id"], "cell": f"{st['lib']}/{st['cell']}", **parasitics.compact(full, net)}
    except KeyError as e:
        raise ToolError(f"{e.args[0]} (job {st['id']})") from e
    fresh = provenance.freshness(c.srv, c.work, st)
    lay = next((e for e in (st.get("inputs") or {}).get("design", []) if e.get("view") == "layout"), {})
    out["outdated"] = fresh["fresh"] is False
    if out["outdated"]:
        out["changed"] = fresh["changed"]
    out["made_from"] = {"layout_sha": (lay.get("sha") or "")[:16] or None,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.get("finished") or st.get("created", 0)))}
    out["use"] = ("simulate on it: run_simulation / check_specs / sweep with extracted: ['" + out["cell"] + "'] (or {'"
                  + out["cell"] + "': '" + st["id"] + "'})" if (full.get("ports") or {}).get("match")
                  else "its ports are not the schematic's: label the missing pins in the layout (ports.missing), separate "
                       "two pins on one net (ports.shorted), and extract again")
    return out


def get_parasitics(c: ToolCtx, a: dict):
    """The newest finished extraction of a cell (of `mode`, or job `job`), as extract_parasitics returns it."""
    lib, cell = _lib_cell(a)
    runs = [r for r in parasitics.jobs_of(c.srv.jobs, c.srv.project(c.work).path, lib, cell)
            if (not a.get("job") or r["id"] == a["job"]) and (not a.get("mode") or r.get("mode") == a["mode"])]
    done = [r for r in runs if r.get("state") == "done"]
    if not done:
        if runs and runs[0].get("state") in ("queued", "running"):
            return {"job": runs[0]["id"], "state": runs[0]["state"],
                    "note": f"still extracting: wait_for job:{runs[0]['id']}{_also(c, ' ')}"}
        if runs:
            raise ToolError(f"the extraction failed (job {runs[0]['id']}): {runs[0].get('error', '')}")
        raise ToolError(f"no parasitic extraction of {lib}/{cell}" + (f" (job {a['job']})" if a.get("job") else "")
                        + ": run extract_parasitics first")
    return _parasitics(c, done[0], a.get("net") or None)


def _extracted(c: ToolCtx, a: dict) -> dict | None:
    """`extracted` of check_specs / sweep as {"lib/cell": extraction job}, resolved once: every point runs on the same
    jobs (run_simulation passes it as asked, so its provenance follows the newest extraction when no job is named)."""
    if not a.get("extracted"):
        return None
    try:
        return {k: st["id"] for k, st in c.srv.extracted_views(c.work, a["extracted"]).items()}
    except (ValueError, KeyError) as e:
        raise ToolError(str(e).strip("'\"")) from e


def run_simulation(c: ToolCtx, a: dict):
    """Runs ngspice and returns what a designer reads off the plots and the log (see simresults.py)."""
    from undine.simulation import simresults

    lib, cell = _lib_cell(a)
    work = c.work
    prj = c.srv.project(work)
    path = prj.library(lib).path / f"{cell}.sch"
    if not path.is_file():
        raise ToolError(f"no schematic {lib}/{cell}")
    analysis = a.get("analysis") or None
    cfg = None
    if a.get("spectrum") is not None:                         # checked before a run that could not give it
        try:
            cfg = spectrum.config(a["spectrum"])
        except ValueError as e:
            raise ToolError(str(e)) from e
        if (why := spectrum.check_run(cfg, analysis)):
            raise ToolError(why)
    sample = None
    if a.get("sample") is not None:                           # also checked before a run that could not give it
        try:
            sample = simresults.sample_config(a["sample"])
        except ValueError as e:
            raise ToolError(str(e)) from e
        if (why := simresults.sample_check(sample, analysis)):
            raise ToolError(why)
    wait = _wait_s(a.get("wait_s"), WAIT_S)
    # measure: .meas statements for ngspice, and measures Undine reads off the run (a name, or derived: settle(...),
    # max(t1, t2), -iavg*1.8), checked before the run
    measures = [a["measure"]] if isinstance(a.get("measure"), str) else a.get("measure") or []    # undine call measure=...
    meas = [m for m in measures if isinstance(m, str) and m.strip().lower().startswith(".meas")]
    reads = [m.strip() if isinstance(m, str) else m for m in measures if m not in meas]
    for m in reads:
        try:
            if not isinstance(m, str) or (len(m.split()) != 1 and not derived.is_derived(m)):
                raise ValueError(f"not {m!r}")
            if derived.is_derived(m):
                derived.check(m)
        except ValueError as e:
            raise ToolError(f"{e}. A measure is one .meas statement on one line (.meas tran ...), or a measure to read off "
                            "the run: a name such as v(out).max, settle(v(out), tol=0.5m, from=10n) or max(t1, t2)") from e
    wanted = specsmod.signals([{"measure": m, "output": None, "input": None, "spectrum": None} for m in reads])
    if a.get("job"):                                          # a run started before: its results, nothing runs again
        given = [k for k in ("analysis", "extracted", "substitute", "time_limit_s", "corner", "temp", "options")
                 if a.get(k) not in (None, "", {}, [])] + (["save_all"] if _flag(a, "save_all") else [])
        if meas:
            given.insert(1, "measure (.meas statements)")
        if given:
            raise ToolError(f"{', '.join(given)} describe a new run; with job, run_simulation reads that job's results"
                            + (" (measures read off the run, such as v(out).max or settle(...), apply to it)" if meas else ""))
        job = _sim_job(c, lib, cell, a["job"])
    else:
        temp = a.get("temp")
        if temp is not None and temp != "":                   # undine call temp=125 sends text
            try:
                temp = float(specsmod._num(temp))
            except (TypeError, ValueError) as e:
                raise ToolError(f"temp is a temperature in °C, e.g. 125, not {temp!r}") from e
        else:
            temp = None
        # what the summary reads: a transient (and a run on an extracted view) stores only these and the testbench's
        # own nets
        save = [*(a.get("signals") or []), *wanted, *(x for x in (a.get("output"), a.get("input")) if x),
                *((cfg or {}).get("signal") or (cfg or {}).get("bits") or []), *((sample or {}).get("signals") or [])]
        try:
            job = c.srv.start_sim(work, lib, cell, analysis, measure=meas or None,
                                  extracted=a.get("extracted") or None, save=save or None, corner=a.get("corner") or None,
                                  temp=temp, options=a.get("options") or None, timeout_s=a.get("time_limit_s"),
                                  substitute=a.get("substitute") or None, save_all=_flag(a, "save_all"))
        except ValueError as e:
            raise ToolError(str(e)) from e
        except KeyError as e:
            raise ToolError(str(e).strip("'\"")) from e
        except PermissionError as e:          # trust.UntrustedContent: the refusal says who can trust what; as it is
            raise ToolError(str(e)) from e
    t0 = time.time()
    while time.time() - t0 < wait and c.srv.jobs.status(job)["state"] in ("queued", "running") and not stopping(c.srv):
        time.sleep(0.5)
    st = c.srv.jobs.status(job)                               # "interrupted": an earlier server's, never to finish
    if st["state"] in ("queued", "running"):                  # not an error: it goes on, and is collected later
        return simresults.pending(job, lib, cell, c.srv.jobs.limit, c.changeset if c.reading else None)
    if st["state"] != "done":
        f = simresults.failure(job, st)
        if st["state"] == "interrupted":
            f["next"] = f"run_simulation(lib='{lib}', cell='{cell}'{_also(c)}) without job runs it again"
        raise ToolError("simulation " + str(f.pop("state") or "failed") + ": " + json.dumps(f, ensure_ascii=False))
    out = simresults.summary(job, a.get("signals"), a.get("output"), a.get("input"), cfg, sample)
    if reads:                                   # one that cannot be read says why, the others still count
        look = out
        if wanted and "signals" in out:         # the signals they read, whichever the summary shows
            extra = simresults.summary(job, wanted).get("signals") or {}
            look = {**out, "signals": {**extra, **out["signals"]}}
        trace, got, why = specsmod.tracer(job), {}, {}
        for m in reads:
            try:
                got[m] = specsmod.value(look, m, trace)
                if got[m] is None:
                    why[m] = specsmod.no_value(out, m)              # a .meas that failed says what ngspice said
            except ValueError as e:
                got[m], why[m] = None, str(e)
        out["measures"] = got
        if why:
            out["measure_errors"] = why
    return out


def _wait_s(v, default: float) -> float:
    try:
        return min(max(float(default if v in (None, "") else v), 0.0), WAIT_JOB_MAX_S)
    except (TypeError, ValueError):
        raise ToolError(f"wait_s is a number of seconds (0 to {WAIT_JOB_MAX_S:g}), not {v!r}") from None


def _find_job(c: ToolCtx, jid: str):
    """(job, the project it is in): your working copy's jobs first, then the project's."""
    if not isinstance(jid, str) or not jid:
        raise ToolError("job: the id a tool returned (e.g. run_simulation's job)")
    for proj in dict.fromkeys([c.work, c.project]):
        try:
            return c.srv.jobs.get(c.srv.project(proj).path, jid), proj
        except KeyError:
            continue
    raise ToolError(f"no job {jid}" + (" in your changeset or the project" if c.changeset else " in the project")
                    + _job_elsewhere(c, jid))


def _sim_job(c: ToolCtx, lib: str, cell: str, jid: str):
    job, _ = _find_job(c, jid)
    st = job.status()
    if st.get("kind") != "sim" or (st.get("lib"), st.get("cell")) != (lib, cell):
        raise ToolError(f"job {jid} is not a simulation of {lib}/{cell}"
                        + (f" (it is {st.get('kind')} of {st.get('lib')}/{st.get('cell')})" if st.get("kind") else ""))
    return job


def cancel_job(c: ToolCtx, a: dict):
    """Stops a queued or running job of yours: in your changeset's working copy, or one you started in the project."""
    job, proj = _find_job(c, a.get("job"))
    st = job.status()
    mine = c.changeset and proj == c.work and not c.reading       # in another's changeset: only what you started
    if not mine and (st.get("owner") is None or st.get("owner") != c.owner):
        raise ToolError(f"job {job.id} is the project's, not one of yours (in your changeset, or started by this session or "
                        "this agent of it); "
                        "the person can stop it in Undine")
    now = c.srv.jobs.status(job)                       # one an earlier server left running is adopted, so it can stop
    state = now.get("state")
    if state not in ("queued", "running"):
        if c.srv.jobs.cancel(job):                     # it ended, but what its process started still runs
            return {"job": job.id, "state": state, "note": "it had ended, but processes it started were still running: "
                    "they are being stopped"}
        return {"job": job.id, "state": state, "note": "it is not running: nothing to stop"
                + (f" ({now.get('error')})" if state == "interrupted" and now.get("error") else "")}
    if not c.srv.jobs.cancel(job):
        raise ToolError(f"job {job.id} is {state} but not in this server's hands; nothing to stop")
    return {"job": job.id, "state": "cancelled", "was": st["state"], "title": st.get("title")}


# ---------------------------------------------------------------------------------- specs (specs.py)


def _spec_probes(c: ToolCtx, lib: str, cell: str, ids) -> list[dict]:
    try:
        model = c.srv.intent_view(c.work, lib, cell)["intent"]
        found = specsmod.probes(model, lib, [ids] if isinstance(ids, str) else ids)
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    except specsmod.SpecError as e:
        raise ToolError(str(e)) from e
    if not found:
        raise ToolError(f"{lib}/{cell} has no specs in its intent: add them with add_intent (kind spec, spec {{measure, "
                        "min and / or max, testbench}), or ask the person for the targets")
    return found


def _corners(c: ToolCtx, corners, whose: str = "") -> list[str] | None:
    if not corners:
        return None
    prj = c.srv.project(c.work)
    known = list(prj.pdk.profile.corners) if prj.pdk else []
    corners = [corners] if isinstance(corners, str) else list(corners)
    if corners == ["all"]:
        corners = known
    if (bad := [x for x in corners if x not in known]):
        raise ToolError(f"{whose}no corner {', '.join(bad)} in this PDK; it has: {', '.join(known) or 'none'}")
    return corners


def _check_vary(c: ToolCtx, lib: str, points: list[dict]) -> None:
    """Every varied instance exists, and has the property (a typo would otherwise sweep nothing, silently)."""
    prj = c.srv.project(c.work)
    res = xschem.SymbolResolver(prj.symbol_paths())
    seen: dict[str, dict] = {}
    wanted = {(cell, inst, prop) for pt in points for cell, insts in pt["overrides"].items()
              for inst, props in insts.items() for prop in props}
    for cell, inst, prop in sorted(wanted):
        if cell not in seen:
            path = next((l.path / f"{cell}.sch" for l in [prj.library(lib), *prj.libraries().values()]
                         if (l.path / f"{cell}.sch").is_file()), None)
            if path is None:
                raise ToolError(f"vary: no schematic {cell}")
            conn = schematic.connectivity(path, res, cell)
            seen[cell] = {d.name: schematic.inst_props(d.comp, d.sym) for d in conn.devices}
        if inst not in seen[cell]:
            raise ToolError(f"vary: {cell} has no instance {inst}; it has {', '.join(sorted(seen[cell])[:30])}")
        props = {k for k in seen[cell][inst] if k not in ("name", "spiceprefix")}
        if prop not in props:
            raise ToolError(f"vary: {cell}/{inst} has no property {prop}; it has {', '.join(sorted(props))}")


def _flag(a: dict, key: str) -> bool:
    """A yes / no argument: true or false, or the text 'true' / 'false' that `undine call key=value` sends."""
    v = a.get(key)
    if v is None or isinstance(v, bool):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    raise ToolError(f"{key} is true or false, not {v!r}")


def _measure(c: ToolCtx, a: dict, vary) -> tuple[list[dict], list[dict], dict, list[str]]:
    lib, cell = _lib_cell(a)
    fresh = _flag(a, "fresh")
    probes = _spec_probes(c, lib, cell, a.get("ids"))
    extracted = _extracted(c, a)
    try:
        subst = sim.substitute_map(a.get("substitute"))
    except ValueError as e:
        raise ToolError(str(e)) from e
    for p in probes:                                            # a spec's own corners, used when the call gives none
        p["corners"] = _corners(c, p["corners"], f"spec {p['id']}: ")
        p["substitute"] = {**p["substitute"], **subst}          # the call's stand-ins over the spec's
    try:
        plan = specsmod.plan(probes, vary, _corners(c, a.get("corners")), a.get("temps"), cell, a.get("conditions"),
                             a.get("mismatch"))
        _check_vary(c, lib, plan["points"])
        res = specsmod.run(c.srv, c.work, probes, plan["points"], SWEEP_WAIT_S, extracted, plan["at"], fresh,
                           a.get("time_limit_s"))
    except specsmod.SpecError as e:
        raise ToolError(str(e)) from e
    except PermissionError as e:              # an untrusted dependency: every point would be refused the same way
        raise ToolError(str(e)) from e
    if extracted:
        res["extracted"] = [{"cell": k, "job": j} for k, j in extracted.items()]
    res.update({"mismatch": plan["mismatch"], "cut": plan["note"], "fresh": fresh})
    return probes, plan["points"], res, plan["design"]


def _tail(out: dict, res: dict) -> dict:
    if res.get("extracted"):
        out["extracted"] = res["extracted"]
    out["runs"] = specsmod.runs_summary(res)
    if out["runs"]["reused"]:
        out["runs"]["note"] = ("reused: runs on exactly the same inputs (netlist, models, options) that had already run, "
                               "when they ran; fresh: true runs them all anew")
    if res.get("cut"):
        out["mismatch_note"] = res["cut"]
    if res["failures"]:
        out["failures"] = res["failures"][:6]
    if res["pending"]:
        out["pending"] = len(res["pending"])
        out["note"] = ("some simulations are still running: call the same tool again with the same arguments to collect "
                       "them (finished runs are reused, nothing runs twice)" if not res.get("fresh") else
                       "some simulations are still running: call the same tool again with the same arguments but without "
                       "fresh to collect them (the newest runs on the same inputs, which this call started, are taken)")
        if (ids := [p["job"] for p in res["pending"] if p.get("job")]):
            out["pending_jobs"] = ids[:12]           # wait_for job:<id> for one of them before asking again
    return out


def _budget(c) -> int | None:
    """The characters the current call's result may have (ToolCtx.limit, set by call; None: whole)."""
    return getattr(c, "limit", RESULT_CHARS)


def _fit(out: dict, *path: str, limit: int | None = RESULT_CHARS) -> dict:
    """`out` with the last items of the list at `path` (the per-point blocks, which come last) left out until it fits in
    the call's result budget (`limit`; None: whole), and how many were: shortened inside by results.py instead, the
    blocks would each lose their ends. What results.py still shortens says so in _truncated."""
    if not limit:
        return out
    holder = out
    for k in path[:-1]:
        holder = holder[k]
    items = holder[path[-1]]
    size, n = len(results.compact(out)), len(items)
    while items and size > limit - 300:
        size -= len(results.compact(items.pop())) + 1
    if len(items) < n:
        holder[f"{path[-1]}_not_shown"] = (f"the last {n - len(items)} of {n}, to fit the answer: ask with fewer specs (ids), "
                                           "corners, temperatures or conditions to see them")
    return out


def _txt(v, limit: int | None = RESULT_CHARS) -> str:
    """A result as the in-app assistant gets it (results.text at RESULT_CHARS)."""
    return results.text(v, limit)


def check_specs(c: ToolCtx, a: dict):
    """Measures the cell's specs on their testbenches (at corners / temperatures) and says which are met, by how much;
    per point the value of each spec, and with mismatch the statistics of the Monte Carlo runs."""
    probes, points, res, _ = _measure(c, a, None)
    out = {"specs": specsmod.summarize(probes, res)}
    met = [s["status"] for s in out["specs"]]
    out["met"] = f"{met.count('met')}/{len(met)}"
    _tail(out, res)                             # runs, pending, failures before the per-point blocks, which are cut to fit
    if res["mismatch"]:
        out["mismatch"] = {**res["mismatch"], "stats": specsmod.mismatch_stats(probes, res["rows"])}
        return _fit(out, "mismatch", "stats", limit=_budget(c))
    if len(points) > 1:
        out["points"] = specsmod.table(res["rows"], probes)
        return _fit(out, "points", limit=_budget(c))
    return out


def _unmeasured(results: dict) -> dict:
    """A sweep row's specs without a value at some of its conditions (specs.fold), each set of conditions with its own
    reason: not_measured (a failed run, a .meas that could not run, still running; never another point's value in its
    place) and no_value (in not_met: a .meas that never triggered, which the spec counts as not met, missing: fail)."""
    out = {}
    if (miss := {k: {"why": v["error"], "at": v["not_measured_at"][:4],
                     **({"worst_measured": v["worst_measured"]["value"]} if v.get("worst_measured") else {})}
                 for k, v in results.items() if "not_measured_at" in v}):
        out["not_measured"] = miss
    if (none := {k: {"why": v["no_value_why"], "at": v["no_value_at"][:4]}
                 for k, v in results.items() if "no_value_at" in v}):
        out["no_value"] = none
    return out


def sweep(c: ToolCtx, a: dict):
    """Measures the cell's specs at every combination of property values (and corners / temperatures), without editing
    the design, and ranks the points by how well they meet the specs."""
    vary = a.get("vary")
    if not vary:
        raise ToolError("vary: the design choices to explore, e.g. {'M1.W': [1, 2, 4]} or {'M1.W': {from: 1, to: 8, "
                        "steps: 4, scale: log}}; operating conditions go in conditions / corners / temps (check_specs "
                        "measures the design as it is)")
    probes, points, res, design = _measure(c, a, vary)
    stats = specsmod.mismatch_stats(probes, res["rows"]) if res["mismatch"] else []
    rows = []
    for r in specsmod.fold(res["rows"], design):
        rows.append({"point": r["point"], **({"score": r["score"], "all_pass": r["all_pass"]} if "score" in r else {}),
                     "values": {k: v.get("value") for k, v in r["results"].items()},
                     "not_met": [k for k, v in r["results"].items() if v.get("pass") is False],
                     **({"worst_at": {k: v["worst_at"] for k, v in r["results"].items() if v.get("worst_at")}}
                        if r["conditions"] > 1 else {})})
        rows[-1].update(_unmeasured(r["results"]))
        if stats:                                    # the design point's lowest yield over its conditions
            per: dict[str, list] = {}
            for g in stats:
                if all(g["at"].get(n) == r["point"].get(n) for n in design):
                    for sid, st in g["specs"].items():
                        per.setdefault(sid, []).append(st)
            rows[-1]["yield"] = {sid: min((st["yield"] for st in sts if st["yield"] is not None), default=None)
                                 for sid, sts in per.items()}
            if (waiting := {sid: n for sid, sts in per.items() if (n := sum(st.get("pending", 0) for st in sts))}):
                rows[-1]["yield_pending"] = waiting  # runs still running: the yield is over the finished ones
    ranked = sorted((r for r in rows if "score" in r), key=lambda r: (not r["all_pass"], -r["score"]))
    out = {"specs": [{k: p[k] for k in ("id", "measure", "min", "max")} for p in probes],
           "best": ranked[:3], "worst_case": specsmod.summarize(probes, res),
           "note": "each design point is judged by its worst case over the conditions, corners and temperatures; score = "
                   "its worst margin relative to the limit (positive: every spec met by that fraction). Nothing was "
                   "changed: set the chosen values with set_instance_params in a changeset, then check_specs."}
    if len(points) > len(rows):
        out["conditions_per_point"] = len(points) // max(len(rows), 1)
    if res["mismatch"]:
        out["mismatch"] = {**res["mismatch"], "note": "yield per design point: its lowest over the conditions, corners "
                           "and temperatures, of the finished runs (yield_pending: runs still running); check_specs with "
                           "mismatch gives the statistics"}
    _tail(out, res)                             # runs, pending, failures before the points, which are cut to fit
    out["points"] = rows
    return _fit(out, "points", limit=_budget(c))


# ---------------------------------------------------------------------------------- RTL (rtl.py)


def get_rtl(c: ToolCtx, a: dict):
    lib, cell = _lib_cell(a)
    try:
        ed = c.srv.rtl_editor(c.work, lib, cell)
        lib_dir = c.srv.project(c.work).library(lib).path
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    text = ed.text()
    if not text:
        raise ToolError(f"{lib}/{cell} has no RTL (create_cell with views [rtl], or write_rtl)")
    return {"file": rtl.find_file(lib_dir, cell).name, "text": text, "ports": rtl.module_ports(text, cell),
            "library_rtl": [f.name for f in rtl.sources(lib_dir)]}


def write_rtl(c: ToolCtx, a: dict):
    lib, cell = _lib_cell(a)
    text = a.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolError("text is required: the whole Verilog / SystemVerilog file")
    if cell not in rtl.modules(text):
        raise ToolError(f"the file must define module {cell} (the cell's name); it defines: {', '.join(rtl.modules(text)) or 'none'}")
    with c.srv.changeset_lock:
        work = c.need_changeset()
        _check_limit(c)
        if not (a.get("reason") or "").strip():
            raise ToolError("reason is required: say why this change is made")
        ed = c.srv.rtl_editor(work, lib, cell)
        with ed.recording("write_rtl", {"chars": len(text)}, c.actor, a["reason"], c.changeset):
            changed = ed.save(text)
        c.writes += 1
    out = {"ok": True, "changed": changed, "ports": rtl.module_ports(text, cell)}
    try:
        from undine.design import gatelevel
        prj = c.srv.project(work)
        lint = rtl.lint(prj.library(lib).path, cell, gatelevel.rtl_inputs(prj, lib, cell))
        out["lint"] = {"ok": lint["ok"], "messages": lint["messages"][:20]}
    except Exception as e:                                       # lint is a help, not a condition of the write
        out["lint"] = {"error": str(e)}
    return out


def _rtl_extra(prj, lib: str, cell: str) -> dict:
    """gatelevel.rtl_inputs: the gate-level netlists of schematic cells the RTL uses and the PDK's cell models."""
    from undine.design import gatelevel
    try:
        return gatelevel.rtl_inputs(prj, lib, cell)
    except (KeyError, ValueError) as e:
        raise ToolError(str(e).strip("'\"")) from e


def lint_rtl(c: ToolCtx, a: dict):
    lib, cell = _lib_cell(a)
    prj = c.srv.project(c.work)
    try:
        return rtl.lint(prj.library(lib).path, cell, _rtl_extra(prj, lib, cell))
    except (KeyError, RuntimeError) as e:
        raise ToolError(str(e).strip("'\"")) from e
    except PermissionError as e:              # trust.UntrustedContent: passed on as it is, a person can trust it
        raise ToolError(str(e)) from e


def run_rtl_sim(c: ToolCtx, a: dict):
    lib, cell = _lib_cell(a)
    prj = c.srv.project(c.work)
    try:
        job = rtl.submit_sim(c.srv.jobs, prj.path, prj.library(lib).path, cell, f"RTL {lib}/{cell}", {"lib": lib, "cell": cell},
                             extra=_rtl_extra(prj, lib, cell))
    except (KeyError, RuntimeError) as e:
        raise ToolError(str(e).strip("'\"")) from e
    except PermissionError as e:              # trust.UntrustedContent: passed on as it is, a person can trust it
        raise ToolError(str(e)) from e
    t0 = time.time()
    while time.time() - t0 < WAIT_S * 4 and job.status()["state"] in ("queued", "running") and not stopping(c.srv):
        time.sleep(0.5)
    st = job.status()
    out = {"job": st["id"], "state": st["state"], **(st.get("summary") or {})}
    if st.get("error"):
        out["error"] = st["error"]
    if st["state"] == "done" and (job.path / "wave.vcd").is_file():
        v = rtl.read_vcd(job.path / "wave.vcd", max_signals=24)
        out["signals"] = {s["name"]: [[t * v["timescale_s"], val] for t, val in v["changes"][s["name"]][-8:]] for s in v["signals"]}
        out["note"] = "signals: the last changes of each signal (time in s, value; None for x / z); the waveform is in the job"
    return out


# ---------------------------------------------------------------------------------- LibreLane (librelane.py)


def list_flows(c: ToolCtx, a: dict):
    prj = c.srv.project(c.work)
    return {"flows": librelane.list_flows(prj.path), "librelane": librelane.available(),
            "note": "a flow configuration is a file of the project (flows/<name>.yaml): propose one with write_flow in "
                    "your changeset (the person reviews and applies it), check one before with check_flow config; "
                    "run_librelane runs a flow as your working copy has it"}


def _flow(c: ToolCtx, name: str, config: dict | None = None):
    """(project, flow, top library's folder, top's intent, gate-level netlist or None) of a saved flow, or of the
    inline configuration `config`."""
    prj = c.srv.project(c.work)
    try:
        flow = librelane.normalize_flow(config) if config is not None else librelane.load_flow(prj.path, name)
    except (KeyError, ValueError) as e:
        raise ToolError(str(e).strip("'\"")) from e
    top = flow.get("top") or {}
    try:
        lib_dir = prj.library(top["lib"]).path if top.get("lib") else None
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    model = c.srv.intent_editor(c.work, top["lib"], top["cell"]).model() if top.get("lib") and top.get("cell") else None
    return prj, flow, lib_dir, model, librelane.gate_netlist(prj, flow, model)


def _flow_check(prj, flow: dict, lib_dir, model, gate) -> dict:
    missing = librelane.requirements(flow, lib_dir, model, prj.path, gate)
    out = {"flow": flow, "missing": [m["message"] for m in missing]}
    if not missing:
        d = librelane.derive(flow, lib_dir, model, prj.pdk.name if prj.pdk else "sky130A", prj.path, gate)
        out["derived"] = d["derived"]
        if d["pin_order"]:
            out["pin_order"] = d["pin_order"]
        if d["ignored"]:
            out["ignored"] = [i["message"] for i in d["ignored"]]
    if gate and not gate.get("error"):
        out["netlist"] = {"from": "schematic", "instances": gate["instances"], "cells": gate["cells"],
                          "warnings": gate["warnings"]}
    return out


def check_flow(c: ToolCtx, a: dict):
    config = a.get("config")
    if config is None and not a.get("flow"):
        raise ToolError("give flow (a saved flow configuration) or config (one to check before proposing it)")
    if config is not None and not isinstance(config, dict):
        raise ToolError("config is a flow configuration: {top: {lib, cell}, source?, clock, floorplan, variables}")
    return _flow_check(*_flow(c, a.get("flow") or "", config))


def write_flow(c: ToolCtx, a: dict):
    name = a.get("flow") or ""
    if not librelane.NAME.match(name):
        raise ToolError("flow is the configuration's name (letters, digits, _ . -), e.g. sar_logic")
    delete = bool(a.get("delete"))
    config = a.get("config")
    if not delete and not isinstance(config, dict):
        raise ToolError("config is required (the flow configuration), or delete: true to take the flow out")
    with c.srv.changeset_lock:
        work = c.need_changeset()
        _check_limit(c)
        if not (a.get("reason") or "").strip():
            raise ToolError("reason is required: say why this change is made")
        prj = c.srv.project(work)
        try:
            flow = librelane.normalize_flow(config) if not delete else librelane.load_flow(prj.path, name)
        except (KeyError, ValueError) as e:
            raise ToolError(str(e).strip("'\"")) from e
        top = flow.get("top") or {}
        if not top.get("lib") or not top.get("cell"):
            raise ToolError("the flow names its top cell: top: {lib, cell}")
        try:
            ed = c.srv.flow_editor(work, top["lib"], top["cell"])
        except (KeyError, ValueError) as e:
            raise ToolError(str(e).strip("'\"")) from e
        with ed.recording("write_flow", {"flow": name, **({"delete": True} if delete else {})}, c.actor, a["reason"],
                          c.changeset):
            changed = ed.save(name, None if delete else flow)
        c.writes += 1
    if delete:
        return {"ok": True, "changed": changed, "deleted": name}
    out = {"ok": True, "changed": changed, "file": f"{librelane.FLOWS_DIR}/{name}.yaml",
           **_flow_check(*_flow(c, name)),
           "note": "the flow is in your changeset: run_librelane runs it there; the person applies it with the rest"}
    return out


def run_librelane(c: ToolCtx, a: dict):
    prj, flow, lib_dir, model, gate = _flow(c, a.get("flow") or "")
    if prj.pdk is None:
        raise ToolError("the project has no PDK")
    try:
        job = librelane.submit(c.srv.jobs, prj.path, lib_dir, a["flow"], flow, model, prj.pdk.root.parent, prj.pdk.name,
                               gate=gate)
    except (ValueError, RuntimeError) as e:
        raise ToolError(str(e)) from e
    except PermissionError as e:              # trust.UntrustedContent: passed on as it is, a person can trust it
        raise ToolError(str(e)) from e
    return {"job": job.id, "state": job.status()["state"],
            "note": "a LibreLane run takes minutes; ask get_librelane_result with this job id"}


def export_verilog(c: ToolCtx, a: dict):
    """The gate-level Verilog of a schematic of standard cells (gatelevel.py)."""
    from undine.design import gatelevel
    lib, cell = _lib_cell(a)
    prj = c.srv.project(c.work)
    try:
        supplies = intent.normalize(c.srv.intent_editor(c.work, lib, cell).model())["supplies"]
    except (KeyError, ValueError):
        supplies = {}
    out = gatelevel.of_cell(prj, lib, cell, supplies)
    if out.get("error"):
        raise ToolError(out["error"])
    return {"file": f"{cell}.v", **out,
            "note": "the schematic's netlist as drawn, power pins inside `ifdef USE_POWER_PINS; a flow with source: "
                    "schematic builds this netlist (LibreLane only elaborates it), lint_rtl / run_rtl_sim read it with "
                    "the PDK's cell models. Nothing was written"}


def get_librelane_result(c: ToolCtx, a: dict):
    prj = c.srv.project(c.work)
    try:
        job = c.srv.jobs.get(prj.path, a.get("job") or "")
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    st = job.status()
    if st.get("kind") != "librelane":
        raise ToolError("not a LibreLane run")
    out = {"state": st["state"], **(st.get("summary") or {})}
    if st.get("error"):
        out["error"] = st["error"]
    if st["state"] in ("queued", "running"):
        out["log_tail"] = job.log(15)
    return out


# ---------------------------------------------------------------------------------- registry


# ------------------------------------------------------------------ pictures (render.py): to look, with the numbers to measure

def render_schematic(c: ToolCtx, a: dict):
    from undine.agents import render

    lib, cell = _lib_cell(a)
    try:
        d = c.srv.editor(c.work, lib, cell, "schematic").view()
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    box = a.get("box")
    if box is not None and (len(box) != 4 or not all(isinstance(v, (int, float)) for v in box) or box[0] == box[2] or box[1] == box[3]):
        raise ToolError("box is [x1, y1, x2, y2] in schematic units (y down, as the editor shows them), with a width and a height")
    if a.get("around"):                               # zoom to instances: their symbols' extent and a margin
        insts = {i["name"]: i["bbox"] for i in d.get("insts") or [] if i.get("name")}
        if (unknown := [n for n in a["around"] if n not in insts]):
            raise ToolError(f"around: no instance {', '.join(map(str, unknown))} in the drawing of {lib}/{cell}")
        bbs = [insts[n] for n in a["around"]]
        x1, y1, x2, y2 = min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs)
        m = max(x2 - x1, y2 - y1, 100) * 0.25
        box = [x1 - m, y1 - m, x2 + m, y2 + m]
    drawn = [min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3])] if box else render.schematic_extent(d)
    png, w, h = render.schematic_png(d, a.get("size") or 1024, drawn)
    return render.Picture(png, {
        "cell": f"{lib}/{cell}", "image": f"{w}x{h} PNG, drawn as in Undine's schematic editor",
        "box": [round(v, 1) for v in drawn], "units_per_px": round(max(drawn[2] - drawn[0], drawn[3] - drawn[1]) / max(w, h), 3),
        "instances": len(d.get("insts") or []), "wires": len(d.get("wires") or []), "ports": d.get("ports"),
        "pending_connections": len(d.get("pending") or []), "missing_symbols": d.get("missing") or [],
        "note": "the picture shows the drawing; what is connected is in get_netlist (pending connections are not drawn). "
                "box: what is drawn, [x1, y1, x2, y2] in schematic units (y down); pass box or around to zoom"})


MARKER_COLOURS = ("#ff3030", "#00e5ff", "#ffd400", "#ff40ff", "#40ff40", "#ff8c00", "#ffffff", "#8080ff")
MARKERS_DRAWN = 500      # render_layout's DRC markers at most


def _markers(c: ToolCtx, lib: str, cell: str, want) -> tuple[list, dict]:
    """The DRC markers render_layout draws (want: true, or rule names), from the cell's latest DRC run of each checker:
    ([(colour, [shape in um], filled)] as LayoutDB.render's overlays, one per rule), {"rule (checker)": {colour, count,
    drawn, job, outdated}})."""
    rules = None if want is True else [str(r) for r in want]
    overlays, legend, drawn = [], {}, 0
    for st in _drc_runs(c, lib, cell, None):
        checker = st.get("tool") or "klayout"
        outdated = provenance.freshness(c.srv, c.work, st)["fresh"] is False
        for r in _drc_report(c, st, rules, MARKERS_DRAWN):
            colour = MARKER_COLOURS[len(legend) % len(MARKER_COLOURS)]
            ms = r["markers"][:max(0, MARKERS_DRAWN - drawn)]
            drawn += len(ms)
            legend[f"{r['rule']} ({checker})"] = {"colour": colour, "count": r["count"], "drawn": len(ms), "job": st["id"],
                                                  **({"outdated": True} if outdated else {})}
            overlays.append((int(colour.lstrip("#"), 16), [s for m in ms for s in _marker_shapes(m)], False))
    return overlays, legend


def _marker_shapes(m: dict) -> list:
    """A marker (verify.marker) as the shapes KLayout's view draws: a box, a polygon, or its edges."""
    import klayout.db as kdb

    if "box" in m:
        return [kdb.DBox(*m["box"])]
    if "polygon" in m:
        return [kdb.DPolygon([kdb.DPoint(x, y) for x, y in m["polygon"]])]
    return [kdb.DEdge(*e) for e in m.get("edges") or [m["edge"]]]


def render_layout(c: ToolCtx, a: dict):
    """A picture of a cell's layout, optionally with nets highlighted (connectivity.Nets: each net's shapes in a colour
    of its own over the dimmed layout), the latest DRC's markers, and the layers filled solid."""
    import klayout.db as kdb
    from undine.agents import render

    work, lib, cell, db = _ldb(c, a, write=False)
    box = a.get("box")
    if box is not None and (not isinstance(box, list) or len(box) != 4):
        raise ToolError("box is [x1, y1, x2, y2] in um")
    overlays, extra = [], {}
    k = db.layout.dbu
    with db.lock:
        if a.get("highlight_nets"):
            prof = _profile(c, work)
            if prof is None or prof.stack is None:
                raise ToolError("highlight_nets needs the PDK's layer stack: this PDK has none in Undine yet (sky130 has)")
            nets = _nets(c, work, lib, cell, db, prof)
            shown, missing = [], []
            for i, name in enumerate(dict.fromkeys(map(str, a["highlight_nets"]))):
                keys = nets.find(name)
                if not keys:
                    missing.append(name)
                    continue
                rgb = render.SERIES[len(shown) % len(render.SERIES)]
                shapes, on = [], []
                for layer in nets.conductors:
                    reg = kdb.Region()
                    for key in keys:
                        reg.insert(nets.shapes(key, layer))
                    if not reg.is_empty():
                        on.append(layer)
                        shapes += [p.to_dtype(k) for p in reg.merged().each()]
                overlays.append((rgb, shapes[:4000], True))
                shown.append({"net": name, "colour": f"#{rgb:06x}", "layers": on, "pieces": len(keys),
                              **({"note": f"{len(keys)} unconnected pieces"} if len(keys) > 1 else {})})
            extra["highlighted"] = shown
            if missing:
                extra["not_found"] = missing
                extra["not_found_note"] = "no net is called that (a label, or the schematic net of a device terminal on it)"
    if a.get("markers"):
        marks, legend = _markers(c, lib, cell, a["markers"])
        overlays += marks
        extra["markers"] = legend
        if not legend:                   # the picture still shows the rest (highlighted nets)
            extra["markers_note"] = (f"no DRC marker of {lib}/{cell}" + ("" if a["markers"] is True else
                                     f" for rules {', '.join(map(str, a['markers']))}") + ": " +
                                     ("the latest runs found none (get_drc_report)" if _drc_runs(c, lib, cell, None)
                                      else "no finished DRC run yet (run_checks)"))
    try:
        png, bb, w, h = db.render(cell, box, a.get("size") or 1024, a.get("layers") or None, int(a.get("depth", 99)),
                                  solid=bool(a.get("solid")), overlays=overlays, dim=bool(a.get("highlight_nets")))
    except KeyError as e:
        raise ToolError(str(e).strip("'\"")) from e
    return render.Picture(png, {
        "cell": f"{lib}/{cell}", "image": f"{w}x{h} PNG with a scale ruler (bottom left)",
        "box_um": [round(v, 3) for v in (bb.left, bb.bottom, bb.right, bb.top)],
        "um_per_px": round(max(bb.width() / w, bb.height() / h), 5), "layers": a.get("layers") or "all", **extra,
        "note": "x grows to the right, y upwards; measure with query_shapes / get_device_geometry, not from the pixels"
                + ("; highlighted nets are drawn solid in their colour over the dimmed layout" if extra.get("highlighted") else "")
                + ("; DRC markers are outlined in the colour of their rule (markers)" if extra.get("markers") else "")})


def _finished_run(c: ToolCtx, a: dict):
    """The testbench's latest finished simulation (or job `job`), the working copy's first: (job record, sim.Job). A
    job by its id, as run_simulation takes it: check_specs / sweep name runs older than the newest 100 (list())."""
    lib, cell = _lib_cell(a)
    if a.get("job"):
        job = _sim_job(c, lib, cell, a["job"])
        st = c.srv.jobs.status(job)
        if st.get("state") in ("queued", "running"):
            raise ToolError(f"simulation {job.id} is {st['state']}: wait_for job:{job.id}{_also(c, ' ')}, then ask again")
        if st.get("state") != "done":
            raise ToolError(f"simulation {job.id} did not finish ({st.get('state')}"
                            + (f": {str(st['error'])[:300]}" if st.get("error") else "") + "), so it has no results to "
                            f"read; run_simulation(job='{job.id}'{_also(c)}) says why")
        return {**st, "id": job.id}, job
    runs = []
    for proj in dict.fromkeys([c.work, c.project]):                 # the working copy's runs first
        prj = c.srv.project(proj)
        runs += [(prj, j) for j in c.srv.jobs.list(prj.path) if j.get("kind") == "sim" and j.get("lib") == lib
                 and j.get("cell") == cell and j.get("state") == "done"]
    if not runs:
        raise ToolError(f"no finished simulation of {lib}/{cell}; run_simulation first")
    prj, rec = runs[0]
    return rec, c.srv.jobs.get(prj.path, rec["id"])


def _x(a: dict, key: str) -> float | None:
    try:
        return None if a.get(key) in (None, "") else spectrum._num(a[key], key, "plot_waveform")
    except ValueError as e:
        raise ToolError(str(e)) from e


def plot_waveform(c: ToolCtx, a: dict):
    from pathlib import Path

    from undine.agents import render
    from undine.simulation import sim, simresults

    rec, job = _finished_run(c, a)
    raws = job.raw_files()
    if not raws:
        raise ToolError(f"simulation {rec['id']} wrote no waveform (raw file)")
    vectors, missing = sim.raw_vectors(Path(raws[0]))[1:], []
    if a.get("signals"):                              # named as run_simulation takes them: out, x1.b9, v(out), i(v1)
        names, missing = simresults.select([n for n, _ in vectors], a["signals"])
    else:                                             # the circuit's own node voltages: no device internals, no currents
        own = [n for n, _ in vectors if "#" not in n and "." not in n.strip("vi()")]
        names = [n for n, t in vectors if n in own and t == "voltage"][:6] or own[:6] or [n for n, _ in vectors][:6]
    if not names:
        have = simresults.own_first([n for n, _ in vectors])
        raise ToolError(f"none of {a['signals']} is in the simulation; it has {', '.join(have[:60])}"
                        + (f" and {len(have) - 60} more" if len(have) > 60 else "")
                        + (f" ({hint})" if (hint := simresults.unsaved(job, a.get("signals"))) else ""))
    window = (_x(a, "tstart"), _x(a, "tstop"))
    window = window if window != (None, None) else None
    try:
        wf = sim.waveform_json(Path(raws[0]), names, window=window, envelope=True)
    except ValueError as e:
        raise ToolError(str(e)) from e
    png, legend = render.waveform_png(wf, (a.get("width") or 1024, a.get("height") or 600))
    out = {"job": rec["id"], "analysis": wf["plot"], "x": {"name": wf["x"]["name"], "from": wf["x"]["values"][0],
                                                          "to": wf["x"]["values"][-1], "points": wf["points"],
                                                          **({"shown": len(wf["x"]["values"])} if window else {})},
           "traces": legend, "image": "PNG plot" + (" (magnitude dB and phase, log frequency)" if wf.get("complex") else ""),
           "note": "for measured figures (gain, delay, bandwidth ...) use run_simulation's results and .meas, not the pixels"}
    if missing:
        out["missing_signals"] = missing
        if (hint := simresults.unsaved(job, missing)):
            out["missing_hint"] = hint
    return render.Picture(png, out)


def sample_waveform(c: ToolCtx, a: dict):
    """Signals of a finished run at given times, at full precision (simresults.samples_of), without running again."""
    from undine.simulation import simresults

    try:
        req = simresults.sample_config({k: a[k] for k in simresults.SAMPLE_FIELDS if k in a})
    except ValueError as e:
        raise ToolError(str(e)) from e
    rec, job = _finished_run(c, a)
    out = simresults.samples_of(job, req)
    if "error" in out:
        raise ToolError(f"{out['error']}" + (f"; it has {', '.join(out['available_signals'])}" if out.get("available_signals") else ""))
    return {"job": rec["id"], **out}


# ------------------------------------------------------------------ waiting for the human side or a job

WAIT_DEFAULT_S, WAIT_MAX_S, WAIT_POLL_S = 60.0, 300.0, 0.5


def _wait_signals(c: ToolCtx, events: list[str]) -> dict:
    """What each event is waiting on, now: compared on every poll; the first that differs has happened."""
    out: dict[str, object] = {}
    for ev in events:
        if ev == "selection":
            out[ev] = (c.srv.selection.get(c.project) or {}).get("t")
        elif ev == "changeset":
            if not c.changeset:
                raise ToolError("changeset: no changeset is open in this session (open_changeset, or wait for job / design / selection)")
            out[ev] = c.cs._load(c.project, c.changeset)["status"]
        elif ev.startswith("job:"):
            out[ev] = _job_state(c, ev[4:])
        elif ev.startswith("design:"):
            lib, _, cell = ev[7:].partition("/")
            if not lib or not cell:
                raise ToolError(f"{ev}: use design:<lib>/<cell>")
            try:
                d = c.srv.project(c.project).library(lib).path
            except KeyError as e:
                raise ToolError(str(e).strip("'\"")) from e
            out[ev] = tuple(sorted((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in d.glob(f"{cell}.*") if f.is_file()))
        else:
            raise ToolError(f"unknown event {ev!r}: selection, changeset, job:<id>, design:<lib>/<cell>")
    return out


def _job_state(c: ToolCtx, jid: str) -> str:
    for proj in dict.fromkeys([c.work, c.project]):
        try:
            return c.srv.jobs.status(c.srv.jobs.get(c.srv.project(proj).path, jid)).get("state", "unknown")
        except KeyError:
            continue
    raise ToolError(f"no job {jid}" + _job_elsewhere(c, jid))


def _also(c: ToolCtx, sep: str = ", ") -> str:
    """What a follow-up a reader's answer names (wait_for, get_parasitics, run_simulation job=) passes, so that it
    reads the same changeset: changeset=<id> (nothing when the caller works in its own)."""
    if not c.reading:
        return ""
    return f"{sep}changeset='{c.changeset}'" if sep == ", " else f"{sep}changeset={c.changeset}"


def _job_elsewhere(c: ToolCtx, jid: str) -> str:
    """For a job not found where the caller looks (its working copy, the project): the changesets whose working copy
    has it, so an agent whose session lost its changeset (a server that forgot it) knows what to open again."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", jid or ""):
        return ""
    found = sorted(p.parents[2].name for p in (c.srv.store.root / ".changesets" / c.project).glob(f"cs-*/work/runs/{jid}")
                   if p.parents[2].name != c.changeset)
    if not found:
        return ""
    return (f"; it is in the working copy of the changeset {', '.join(found)}"
            + ("" if not c.changeset else f", not in yours ({c.changeset})")
            + f": pass changeset={found[0]} to read it there (wait_for, run_simulation, get_parasitics ...)"
            + ("; if it is the changeset you write and this session lost it, open_changeset id=" + found[0]
               + " continues it" if not c.changeset else ""))


def wait_for(c: ToolCtx, a: dict):
    """Blocks until one of the events happens (or the timeout): the human side and jobs, without polling from outside."""
    events = a.get("events") or []
    if isinstance(events, str):
        events = [events]
    if not events:
        raise ToolError("events: e.g. ['changeset'] (applied or discarded), ['selection'], ['job:<id>'], ['design:mylib/inv']")
    jobs = any(ev.startswith("job:") for ev in events if isinstance(ev, str))
    timeout = min(max(float(a.get("timeout_s") or WAIT_DEFAULT_S), 1.0), WAIT_JOB_MAX_S if jobs else WAIT_MAX_S)
    start = _wait_signals(c, events)
    for ev, v in start.items():                     # a job that is already over needs no waiting
        if ev.startswith("job:") and v not in ("queued", "running"):
            return {"event": ev, "state": v, "waited_s": 0, **_job_next(c, ev[4:], v)}
    t0, now = time.time(), start
    while time.time() - t0 < timeout and not stopping(c.srv):
        time.sleep(WAIT_POLL_S)
        now = _wait_signals(c, events)
        for ev in events:
            # a job's status read while it is rewritten is "unknown" for a moment: not an end
            if now[ev] != start[ev] and not (ev.startswith("job:") and now[ev] in ("queued", "running", "unknown")):
                out = {"event": ev, "waited_s": round(time.time() - t0, 1)}
                if ev == "selection":
                    out["selection"] = get_selection(c, {})
                elif ev == "changeset":
                    out["status"] = now[ev]
                elif ev.startswith("job:"):
                    out.update(state=now[ev], **_job_next(c, ev[4:], now[ev]))
                else:
                    out["note"] = "the cell changed in the real design; read it again (get_netlist / get_design_context)"
                return out
    if stopping(c.srv) and getattr(c.srv, "stop_jobs", False):      # `undine serve --stop-jobs`: they end with it
        out = {"event": None, "stopping": True, "waited_s": round(time.time() - t0, 1),
               "note": "the server is stopping, and its simulations with it (--stop-jobs): they end cancelled; run "
                       "them again once Undine is back"}
    elif stopping(c.srv):                           # a restart: the simulations go on in the next server, waited for there
        ending = [ev[4:] for ev in events if ev.startswith("job:") and _job_kind(c, ev[4:]) not in (None, "sim")]
        out = {"event": None, "stopping": True, "waited_s": round(time.time() - t0, 1),
               "note": "the server is stopping; its simulations go on, and the next server on its projects takes them "
                       "up (a restart loses none): wait again in a few seconds (a call made while it restarts waits for "
                       "it)"}
        if ending:                                  # a DRC, LVS, extraction, RTL or LibreLane run: it ends with the server
            sims = len(ending) < sum(ev.startswith("job:") for ev in events)
            out["note"] = ("the server is stopping, and these jobs end with it (they are not simulations, and the next "
                           "server takes up simulations only): they show as interrupted there; run them again once "
                           "Undine is back (a few seconds)" + ("; the simulations go on in the next server, wait for "
                                                              "them again there" if sims else ""))
            out["ended"] = ending
    else:
        out = {"event": None, "timeout": True, "waited_s": round(timeout, 1),
               "note": "nothing happened; wait again or carry on"}
    if jobs:                                        # where the jobs are: still queued, or running
        out["jobs"] = {ev[4:]: v for ev, v in now.items() if ev.startswith("job:")}
    return out


# the tool that runs a job of another kind again (_job_next), by its kind
RUN_AGAIN = {"drc": "run_checks", "lvs": "run_checks", "symbol-check": "run_checks", "pex": "extract_parasitics",
             "rtl-sim": "run_rtl_sim", "librelane": "run_librelane"}


def _job_kind(c: ToolCtx, jid: str) -> str | None:
    """A job's kind ("sim" for a simulation), None for one not found."""
    try:
        return _find_job(c, jid)[0].status().get("kind", "sim")
    except ToolError:
        return None


def _job_next(c: ToolCtx, jid: str, state: str) -> dict:
    """For a simulation that ended: how to read its results (or that it is to run again: interrupted). For a job of
    another kind that was interrupted (its server stopped: no later server takes it up), why, and what runs it again."""
    try:
        job = _find_job(c, jid)[0]
    except ToolError:
        return {}
    st = job.status()
    if st.get("kind", "sim") != "sim" and state == "interrupted":
        again = RUN_AGAIN.get(st.get("kind"))
        return {"note": c.srv.jobs.status(job).get("error") or sim.INTERRUPTED["other"],
                "next": f"{again} runs it again" if again else "run it again"}
    if st.get("kind") != "sim" or state not in ("done", "failed", "interrupted"):
        return {}
    if state == "interrupted":
        return {"note": c.srv.jobs.status(job).get("error") or sim.INTERRUPTED["running"],
                "next": f"run_simulation(lib='{st.get('lib')}', cell='{st.get('cell')}'{_also(c)}) runs it again"}
    return {"next": f"run_simulation(lib='{st.get('lib')}', cell='{st.get('cell')}', job='{jid}'{_also(c)}) for its results"
                    + (" or why it failed" if state == "failed" else "")}


# ------------------------------------------------------------------ several edits as one step

BATCH_EXCLUDED = {"open_changeset", "list_changesets", "get_changeset", "batch", "run_checks", "run_simulation",
                  "run_rtl_sim", "run_librelane", "get_librelane_result", "wait_for", "check_specs", "sweep",
                  "extract_parasitics", "cancel_job", "restore_changeset"}   # changeset management and jobs: not in a step
BATCH_MAX = 2000         # a CDAC's shapes in one step: 2000 add_box take about 4 s on sky130A (bench_layout_assembly.py)


def batch(c: ToolCtx, a: dict):
    """Several tool calls in the changeset as one all-or-nothing step (Changesets.batch); with dry_run nothing is kept."""
    c.need_changeset()
    ops, dry = a.get("ops"), bool(a.get("dry_run"))
    if not isinstance(ops, list) or not ops:
        raise ToolError("ops must be a non-empty list of {tool, args}")
    if len(ops) > BATCH_MAX:
        raise ToolError(f"at most {BATCH_MAX} ops per step; split the work")
    ops = list(ops)                                   # their checked arguments replace the given ones
    for i, op in enumerate(ops):
        name = op.get("tool") if isinstance(op, dict) else None
        if name not in BY_NAME:
            raise ToolError(f"ops[{i}]: unknown tool {name!r}")
        if name in BATCH_EXCLUDED or BY_NAME[name].get("picture"):
            raise ToolError(f"ops[{i}]: {name} cannot be part of batch (only edits and reads that answer in text); call it on its own")
        if not isinstance(op.get("args", {}), dict):
            raise ToolError(f"ops[{i}]: args must be an object")
        if (op.get("args") or {}).get("changeset") not in (None, "", c.changeset):   # a step is in your changeset
            raise ToolError(f"ops[{i}]: changeset reads another's changeset; a batch runs in yours: call {name} with "
                            "changeset on its own")
        op = {**op, "args": {k: v for k, v in (op.get("args") or {}).items() if k != "changeset"}}
        try:
            op = ops[i] = {**op, "args": _args(BY_NAME[name], op.get("args") or {})}
        except ToolError as e:
            raise ToolError(f"ops[{i}] {name}: {e}") from e

    progress = {"ops": len(ops), "done": 0, "by": c.actor, "started": time.time()}

    def run():
        out = []
        c.srv.batch_progress[c.changeset] = progress    # get_changeset says how far, while it runs
        for i, op in enumerate(ops):
            progress["done"] = i
            name, args = op["tool"], dict(op.get("args") or {})
            if BY_NAME[name]["write"] and a.get("reason") and not args.get("reason"):
                args["reason"] = a["reason"]
            try:
                res = BY_NAME[name]["fn"](c, args)
            except (ToolError, live.WriteLocked) as e:
                raise ToolError(f"ops[{i}] {name} failed: {e}. Nothing was kept: the whole step was taken back") from e
            except Exception as e:
                import traceback
                traceback.print_exc()
                raise ToolError(f"ops[{i}] {name}: internal error: {e}. Nothing was kept: the whole step was taken back") from e
            out.append({"tool": name, "result": res})
        return out

    try:
        r = c.cs.batch(c.project, c.changeset, run, dry_run=dry)
    except ChangesetError as e:
        raise ToolError(str(e)) from e
    finally:
        if c.srv.batch_progress.get(c.changeset) is progress:
            c.srv.batch_progress.pop(c.changeset, None)
    r = _batch_summary(r, llm_context.layer_names(c.srv, c.work))
    if dry:
        return {"dry_run": True, "note": "nothing was kept; call batch again without dry_run to make these changes", **r}
    return {"applied": len(ops), **r}


BATCH_LISTED = 40        # results listed one by one up to here (an answer holds RESULT_CHARS)
WARNING_KEYS = ("warning", "warnings", "errors")       # what a result says that a long batch's answer never leaves out


def _batch_summary(r: dict, names: dict | None = None) -> dict:
    """A batch's answer, at any size: consecutive ops of one tool that answered the same are one result (ops "3-35"),
    the first BATCH_LISTED // 2 results when there are more than BATCH_LISTED and the warnings of the others (by op
    index; the first BATCH_LISTED // 2, counted beyond); what it changed per reason (one, unless ops gave their own),
    its operations counted and per cell its effects ("add box met1 ×20", an instance by name and place)."""
    results: list[dict] = []
    for i, x in enumerate(r["results"]):
        if results and results[-1]["tool"] == x["tool"] and results[-1]["result"] == x["result"]:
            results[-1]["last"] = i
        else:
            results.append({"op": i, "tool": x["tool"], "result": x["result"]})
    for e in results:
        if "last" in e:
            e["op"] = f"{e['op']}-{e.pop('last')}"
    groups: dict = {}                                   # per reason (ops given none take the batch's): _step
    for ch in r["changes"]:
        g = groups.setdefault(ch["reason"], {"ops": {}, "cells": {}})
        g["ops"][ch["op"]] = g["ops"].get(ch["op"], 0) + 1
        g["cells"].setdefault((ch["cell"], ch["view"]), []).extend(ch["effects"])
    changes = [{**_step(g["ops"], g["cells"], names), **({"reason": why} if why else {})} for why, g in groups.items()]
    if len(results) <= BATCH_LISTED:
        return {"results": results, "changes": changes}
    shown = BATCH_LISTED // 2
    first = int(str(results[shown]["op"]).split("-")[0])
    warned = [{"op": i, "tool": x["tool"], **{k: res[k] for k in WARNING_KEYS if res.get(k)}}
              for i, x in enumerate(r["results"][first:], first)
              if isinstance(res := x["result"], dict) and any(res.get(k) for k in WARNING_KEYS)]
    out = {"results": results[:shown], "results_left_out": len(r["results"]) - first, "changes": changes}
    if warned:
        out["warnings"] = warned[:shown]
        if len(warned) > shown:
            out["warnings_left_out"] = len(warned) - shown
    return out


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


S = {"type": "string"}
FLOW_CONFIG = {"type": "object", "description": "a flow configuration: {top: {lib, cell}, source?: rtl | schematic, "
               "clock: {port, period_ns}, floorplan: {die_area_um | core_util}, variables: {...}, caravel?: {...}}"}
LIBCELL = {"lib": {"type": "string", "description": "library name, e.g. mylib"}, "cell": {"type": "string", "description": "cell name, e.g. inv"}}
REASON = {"reason": {"type": "string", "description": "why this change is made (kept in the history, shown to the reviewer)"}}
STYLE = {"style": {"type": "string", "enum": list(STYLES), "description": "auto (default): wires, labels for supplies / ground and large nets; wire; label"}}
MOVE = {"move": {"type": "boolean", "description": "a pin drawn to another net is taken off it first (disconnect_pins), in the same step"}}
OBJ = {"type": "object", "properties": {"kind": {"type": "string", "enum": ["instance", "net", "wire"]}, "id": S}, "required": ["kind", "id"],
       "additionalProperties": False}
NAMED = {"type": "string", "description": "a name: an instance (M1), a code block, or a net (out)"}
SAMPLE = {"signals": {"type": "array", "items": S, "description": "nodes, as in signals: ['outp', 'outn'], ['x1.b9']"},
          "times": {"type": "array", "items": {"type": "number"}, "description": "the times to sample, in s"},
          "start": {"type": "string", "description": "or regular times: the first, e.g. '1.5u' (default 0)"},
          "fs": {"type": "string", "description": "the sample rate, e.g. '10Meg' (or period)"},
          "period": {"type": "string", "description": "the time between samples, e.g. '100n'"},
          "points": {"type": "integer", "description": f"how many (at most {1 << 16})"}}


NUM = {"type": "number"}
END = {"anyOf": [S, {"type": "array", "items": NUM}],
       "description": "a point [x, y] (um), a device terminal 'XMS:S', or a net by its label / schematic name"}
EXTRACTED = {"extracted": {"anyOf": [{"type": "array", "items": S}, {"type": "object", "additionalProperties": S}],
                           "description": "post-layout: cells simulated as extracted from their layouts (extract_parasitics) "
                                          "in place of their schematics, e.g. ['adc/cdac', 'adc/comparator'] (each its "
                                          "newest up-to-date extraction) or {'adc/cdac': '<extraction job>' or 'c' / 'rc'}. "
                                          "Refused when the extraction is missing, outdated or its ports are not the schematic's. "
                                          "The run stores the testbench's nets and what is measured, not the extracted cells' "
                                          "inner nodes: name one in signals (or a spec's measure) to keep it"}}
SUBSTITUTE = {"substitute": {"type": "object", "additionalProperties": {"anyOf": [S, {"type": "null"}]},
                             "description": "stand-ins: every instance of a cell, at any depth below the testbench, netlisted "
                                            "as another cell with the same pins in the same order (a behavioural model of "
                                            "it), e.g. {'sar/sar_logic': 'sar/sar_logic_ideal'} for a long ADC transient. "
                                            "Over the testbench's default (a substitute rule in its intent) and, in "
                                            "check_specs / sweep, each spec's own; null takes one back "
                                            "({'sar/sar_logic': null}: the cell itself). "
                                            "Refused when the pins differ (named) or a stand-in contains the cell it "
                                            "replaces; the result names the stand-ins used (substitute) and warns of one "
                                            "that replaced nothing (substitute_warnings)"}}
TIME_LIMIT = {"time_limit_s": {"type": "number", "description": f"stop a run still going after this many seconds (default "
                               f"{sim.TIMEOUT_S:g}, at most {sim.TIMEOUT_MAX_S:g}): a long transient (an ADC's conversions) "
                               "needs more than the default"}}
OPTIONS = {"options": {"type": "object", "description": "simulator options for the run: reltol, abstol, vntol, chgtol, "
                       "trtol, gmin, method (gear | trap), maxord, itl1, itl4, and maxstep (the transient's largest time "
                       "step), e.g. {reltol: 1e-6, maxstep: '0.2n'}; to check the numerical floor"}}
SPEC_ARGS = {"ids": {"type": "array", "items": S, "description": "only these specs (default: every spec of the cell)"},
             "conditions": {"type": "object", "description": "operating conditions, swept like corners (the worst case counts): "
                            "cell/instance.property -> values, e.g. {'tb_ac/V1.value': ['1.62', '1.98'], 'tb_ac/CL.value': ['1p', '5p']}, "
                            "and simulator options as options.<name>, e.g. {'options.reltol': ['1e-3', '1e-6'], "
                            "'options.maxstep': ['1n', '0.2n']} (default: each spec's own conditions)"},
             "corners": {"type": "array", "items": S, "description": "PDK model corners, e.g. ['tt', 'ss', 'ff'], or ['all'] (default: each spec's own corners, else as the testbench is)"},
             "temps": {"type": "array", "items": {"type": "number"}, "description": "temperatures in °C, e.g. [-40, 27, 125] (default: each spec's own temps, else as the testbench is)"},
             "fresh": {"type": "boolean", "description": "run every simulation anew instead of reusing finished runs on the same "
                       "inputs, e.g. to verify someone else's design independently (the result says which runs were reused "
                       "and when they ran)"},
             "mismatch": {"type": "object", "properties": {"runs": {"type": "integer", "minimum": 1,
                                                                    "maximum": specsmod.MAX_MISMATCH_RUNS},
                                                           "seed": {"type": "integer", "minimum": 1, "maximum": specsmod.MAX_SEED}},
                          "description": "Monte Carlo device mismatch (the PDK's mismatch models, sky130: MC_MM_SWITCH): "
                                         "each point simulated runs times, seeds seed .. seed + runs - 1 (seed default 1; "
                                         "ngspice takes seeds from 1 only, and the same seed gives the same numbers); "
                                         "per spec the mean, sigma, min / max, yield against its limits and the worst "
                                         f"seed. Cut to fit {specsmod.MAX_JOBS} simulations per call (the result says so); "
                                         "the runs wait their turn in the server's job queue like any other"},
             **EXTRACTED, **SUBSTITUTE, **TIME_LIMIT}


TOOLS: list[dict] = [
    {"name": "get_selection", "write": False, "fn": get_selection,
     "description": "What the user has selected in the Undine GUI right now (object ids, cell, view). Use it to resolve 'this transistor'. Check `stale`.",
     "input_schema": _obj({})},
    {"name": "get_design_context", "write": False, "fn": get_design_context,
     "description": "Parameters, pin-to-net connectivity, neighbours, latest DRC/LVS results and recent changes for a cell and optionally specific objects (listed first: names such as ['M1', 'out'], or {kind, id}; a code block shows its text). Reflects your changeset once one is open.",
     "input_schema": _obj({**LIBCELL, "objects": {"type": "array", "items": {"anyOf": [NAMED, OBJ]}},
                           "overview": {"type": "boolean", "description": "the cell's devices too (default true)"}}, ["lib", "cell"])},
    {"name": "list_cells", "write": False, "fn": list_cells, "description": "Cells (with their views) of the project's libraries.",
     "input_schema": _obj({"lib": S})},
    {"name": "get_checks", "write": False, "fn": get_checks, "description": "Latest DRC / LVS results of a cell.",
     "input_schema": _obj(LIBCELL, ["lib", "cell"])},
    {"name": "get_history", "write": False, "fn": get_history, "description": "Recent recorded changes of a cell (who, what, why): its last limit steps (default 15), a batch of any size as one.",
     "input_schema": _obj({**LIBCELL, "limit": {"type": "integer"}}, ["lib", "cell"])},
    {"name": "get_layout_info", "write": False, "fn": get_layout_info,
     "description": "Layout of a cell: size, bound device instances (arrays with their columns, rows and pitch; placed_with: "
                    "a piece you placed with place_pcell / place_array), unbound instances by id (child@x,y, the origin in "
                    "um) and counted by cell, shape counts per layer, and how it compares with the schematic (missing / "
                    "out-of-date devices, own_layout: devices that sync_layout leaves alone, taps). wiring (sky130): the "
                    "drawn conductors at each placed device's terminals: unrouted_nets (the substrate and the n-wells join "
                    "nothing, being no wires; substrate_only / well_only: those only they would join, which LVS still "
                    "matches), body_ties (a body without a tap of its own, bulk none, tied by a tap of its net in its "
                    "substrate region or well within the latch-up distance; bodies_untied: why not), shorts (on: "
                    "nwell when a well joins two nets), butted_taps (a tap "
                    "butted against a source / drain on another net). Coordinates in micrometres.",
     "input_schema": _obj(LIBCELL, ["lib", "cell"])},
    {"name": "get_device_geometry", "write": False, "fn": get_device_geometry,
     "description": "A placed device's shapes and terminals, in absolute um: every layer it draws (or layers: any layer by "
                    "name, e.g. capm, hvi, npc, areaid.sc, or 'layer/datatype'; an unknown one is refused) as the exact "
                    "rectangles of its merged shapes (polygons: outlines instead), contacts and vias as arrays (count, size, "
                    "columns x rows, pitch, extent; irregular with where the columns / rows are when unevenly spaced); and its terminals (a MOSFET's S / G / D / B, a MIM capacitor's c0 top "
                    "and c1 bottom plate) with their shapes, the net each is on in the layout now (labels, and the "
                    "schematic nets of the device terminals on it) and in the latest LVS, and a source / drain that abuts "
                    "the body tap. A bound array: its first element. instance: a schematic instance (a device placed as "
                    "several place_pcell pieces: all of them, listed in pieces) or one piece's id (child@x,y). A "
                    "multi-finger PCell placed as it is (nf 2 ...) has its terminals too, each the strips of all its "
                    "fingers (nets while they are not joined yet). route and add_via take the terminals as 'XMS:S' or "
                    "'child@x,y:S'. "
                    "On a PDK whose layer stack Undine lacks (sky130, GF180MCU and IHP have one): terminals null, with "
                    "terminals_note.",
     "input_schema": _obj({**LIBCELL, "instance": S,
                           "layers": {"type": "array", "items": S, "description": "only these layers (default: all it draws)"},
                           "polygons": {"type": "boolean", "description": "each merged shape's outline [[x, y], ...] instead of rectangles"},
                           "terminals": {"type": "boolean", "description": "the terminals and their nets (default true)"}},
                          ["lib", "cell", "instance"])},
    {"name": "query_shapes", "write": False, "fn": query_shapes,
     "description": "Shapes of the cell's own layout (not inside device cells) lying completely inside a box [x1,y1,x2,y2] in um, optionally on one layer.",
     "input_schema": _obj({**LIBCELL, "box": {"type": "array", "items": {"type": "number"}}, "layer": S}, ["lib", "cell", "box"])},
    {"name": "get_lvs_report", "write": False, "fn": get_lvs_report,
     "description": "Details of the latest LVS run of a cell in the current design: which devices / pins / nets do not match, the netlist extracted from the layout and the schematic's reference netlist; on a mismatch, why: device counts by class in the layout and the schematic, devices and pins without a counterpart, unconnected layout nets, or that KLayout kept no circuit of the layout (its devices not wired to each other, no pins). Use it to find why LVS fails.",
     "input_schema": _obj(LIBCELL, ["lib", "cell"])},
    {"name": "check_device_integrity", "write": False, "fn": check_device_integrity,
     "description": "Find shapes drawn in the cell that overlap a placed device's transistor area (e.g. a poly bar joining two gates that reaches into the device's diffusion). That makes LVS extract a different L / W than the schematic. Use it when LVS shows odd device sizes.",
     "input_schema": _obj({**LIBCELL, "instance": S}, ["lib", "cell"])},
    {"name": "list_symbols", "write": False, "fn": list_symbols,
     "description": "Symbols that can be placed in a schematic, as refs like sky130_fd_pr/nfet_01v8.sym (filter with query / lib). With ref: that symbol's pin names and default parameters.",
     "input_schema": _obj({"query": S, "lib": S, "ref": S})},
    {"name": "create_cell", "write": True, "fn": create_cell,
     "description": "Create a new cell in a project library of your changeset: views is a list of schematic, symbol, rtl and / or layout (default schematic); rtl starts from a module template (or pass rtl: the file text). Nothing exists in the real project until a person applies the changeset. Create the symbol after the schematic has its ports. Then build the cell with add_instance / add_wire / set_instance_params.",
     "input_schema": _obj({"lib": S, "cell": S, "views": {"type": "array", "items": {"type": "string", "enum": ["schematic", "symbol", "rtl", "layout"]}},
                           "rtl": {"type": "string", "description": "with views [rtl]: the file text (else a module template)"}, **REASON}, ["lib", "cell", "reason"])},
    {"name": "get_changeset", "write": False, "fn": get_changeset,
     "description": "A changeset's record, its operations as the steps they were made in (a tool call, a batch is one; "
                    "steps alike as one, steps: n), each with who made it, why, and what it changed per cell, counted (cells "
                    "it only created in the step's new_cells), and its check results: yours, or with id any changeset of the project. An "
                    "open one lists at the top every cell it created that is still there (new_cells: lib/cell -> views, the "
                    "PCell variants and the _par / _tie cells round them too): every other cell is the baseline's. An open one says who is writing it (writer, "
                    "and whether that is you); a closed one (applied, discarded, reverted) how it was closed (when, by whom, "
                    "the merge commit, or why it was left uncommitted) and the evidence archived then, to verify what was applied. "
                    "An open one lists its latest operations as restore_points (newest first, after: the point right "
                    "after each) for restore_changeset: the last 12, restore_points_more says how many older ones there "
                    "are; restore_points: n or 'all', and restore_before: a point (only those before it) reach every one; "
                    "restore_point_now is where it is (points count every record, operations those in effect). While a "
                    "batch is still being applied, in_progress says how far (done of ops): its operations are not "
                    "counted or listed until it ends; do not edit before then.",
     "input_schema": _obj({"id": {"type": "string", "description": "a changeset id, e.g. cs-1a2b3c (default: yours)"},
                           "restore_points": {"anyOf": [{"type": "integer"}, {"type": "string", "enum": ["all"]}],
                                              "description": "how many restore points to list (default 12), or 'all'"},
                           "restore_before": {"type": "integer", "description": "list the restore points before this one "
                                                                                "(an after from an earlier answer): the older ones"},
                           "cell": {"type": "string", "description": "only this cell's operations (and their count), as lib/cell"},
                           "detail": {"type": "boolean", "description": "each operation on its own (default: one entry per "
                                                                        "step, a batch is one, its effects counted)"}})},
    {"name": "restore_changeset", "write": True, "fn": restore_changeset,
     "description": "Bring your changeset's working copy back to a restore point: to = after of an operation in "
                    "get_changeset's restore_points (the state right after it), or 0 (as opened). The operations after it "
                    "are taken back, all of them in every cell, and recorded as one restore (the history keeps them; a "
                    "restore can itself be restored away). Cells and libraries they created go too when nothing else uses them.",
     "input_schema": _obj({"to": {"type": "integer"}}, ["to"])},
    {"name": "open_changeset", "write": True, "fn": open_changeset,
     "description": "Open a changeset (a git branch of the design and its working copy) before making any edit. Nothing touches the real design until a person reviews and applies it. With id: continue an open changeset (list_changesets) instead of starting a new one.",
     "input_schema": _obj({"title": S, "reason": S, "id": S})},
    {"name": "list_changesets", "write": False, "fn": list_changesets,
     "description": "The project's open changesets (proposals not yet applied or discarded), to continue one with "
                    "open_changeset(id): who opened each, which is yours, and who is writing it now (writer: another "
                    "agent's is not yours to write).",
     "input_schema": _obj({})},
    {"name": "render_schematic", "write": False, "picture": True, "fn": render_schematic,
     "description": "A picture (PNG) of a cell's schematic as Undine's editor draws it, with its counts (instances, wires, ports, "
                    "pending connections). In your working copy once a changeset is open. To look at the drawing: placement, "
                    "crossings, readability. What is connected is get_netlist. To look closer at a large one: around (instances "
                    "to zoom to) or box; the answer gives the box drawn.",
     "input_schema": _obj({**LIBCELL, "size": {"type": "integer", "description": "longer side in pixels (default 1024, at most 2048)"},
                           "around": {"type": "array", "items": S, "description": "zoom to these instances, e.g. ['xgn2', 'xseq2']"},
                           "box": {"type": "array", "items": {"type": "number"}, "description": "[x1, y1, x2, y2] in schematic units (y down), as a previous answer's box"}},
                          ["lib", "cell"])},
    {"name": "render_layout", "write": False, "picture": True, "fn": render_layout,
     "description": "A picture (PNG) of a cell's layout with the PDK's layer colours and a scale ruler: the whole cell, or box "
                    "[x1,y1,x2,y2] in um, optionally only some layers (['li1', 'met1', 'mcon'] or '68/20'). highlight_nets "
                    "draws those nets' shapes solid, each in its own colour, over the dimmed layout (a net by its label or "
                    "the schematic net of a device terminal on it, from a connectivity extraction of the layout now); "
                    "markers: true (or rule names) draws the DRC markers of the cell's latest runs (each checker's), "
                    f"a colour per rule (the answer's markers: rule -> colour, count, job, outdated; at most {MARKERS_DRAWN}; "
                    "markers_note when there are none); "
                    "solid fills every layer solid. Returns the box drawn and um per pixel. To look at placement and "
                    "routing; measure with query_shapes / get_device_geometry.",
     "input_schema": _obj({**LIBCELL, "box": {"type": "array", "items": {"type": "number"}},
                           "layers": {"type": "array", "items": S}, "size": {"type": "integer", "description": "longer side in pixels (default 1024)"},
                           "depth": {"type": "integer", "description": "hierarchy levels drawn (default all)"},
                           "highlight_nets": {"type": "array", "items": S, "description": "nets to highlight, e.g. ['vin', 'vout']"},
                           "markers": {"anyOf": [{"type": "boolean"}, {"type": "array", "items": S}],
                                       "description": "true: draw the DRC markers of the latest runs; or the rules to "
                                                      "draw, e.g. ['capm.11', 'm3.3cd']"},
                           "solid": {"type": "boolean", "description": "every layer filled solid (no stipples)"}}, ["lib", "cell"])},
    {"name": "get_drc_report", "write": False, "fn": get_drc_report,
     "description": "Where the DRC violations of a cell are: its latest DRC run of each checker (the PDK's KLayout deck, and "
                    "Magic's drc(full) where run_checks runs it), each with its job, when it finished and whether it is "
                    "outdated, and per rule with violations its description, count and markers in um (boxes, the edge "
                    "pairs a spacing / width rule measured, polygons). In your working copy once a changeset is open. "
                    "Then fix them; render_layout(markers: true) draws them.",
     "input_schema": _obj({**LIBCELL, "rule": {"anyOf": [S, {"type": "array", "items": S}],
                                               "description": "only this rule (or these), e.g. 'capm.11'"},
                           "checker": {"type": "string", "enum": list(CHECKERS), "description": "only this checker's run"},
                           "limit": {"type": "integer", "description": f"markers per rule (default {DRC_MARKERS}, at most 200)"}},
                          ["lib", "cell"])},
    {"name": "plot_waveform", "write": False, "picture": True, "fn": plot_waveform,
     "description": "A plot (PNG) of a testbench's latest finished ngspice run (or job), with each trace's colour and range: "
                    "transient / DC as one panel, AC as magnitude (dB) and phase over log frequency. signals default to the "
                    "circuit's own nets. The figures to report come from run_simulation (and .meas), the plot is to look. "
                    "tstart / tstop show a stretch of the run (a narrow spike stays visible in a long one).",
     "input_schema": _obj({**LIBCELL, "job": S, "signals": {"type": "array", "items": S, "description": "as in run_simulation: ['out', 'x1.b9'] or v(out), i(v1)"},
                           "tstart": {"type": "string", "description": "from, e.g. '390n' (the x axis: time, or frequency / the swept value)"},
                           "tstop": {"type": "string", "description": "to, e.g. '392n'"},
                           "width": {"type": "integer"}, "height": {"type": "integer"}}, ["lib", "cell"])},
    {"name": "sample_waveform", "write": False, "fn": sample_waveform,
     "description": "Signals of a testbench's latest finished transient run (or job) at full precision, without running it "
                    "again: at times [...] or at start + k / fs (k < points, as run_simulation's spectrum samples), as rows "
                    "[time, value, ...]. To check a figure yourself (an FFT, a code), instead of many .meas lines. Values "
                    "between the simulator's time points are interpolated linearly.",
     "input_schema": _obj({**LIBCELL, "job": S, **SAMPLE}, ["lib", "cell", "signals"])},
    {"name": "wait_for", "write": False, "fn": wait_for,
     "description": "Wait until something happens on the human side or a job finishes, instead of polling: events "
                    "'changeset' (the person applies or discards your changeset), 'selection' (they select something in "
                    "Undine: 'look at this'), 'job:<id>' (a run_librelane or other job ends), 'design:<lib>/<cell>' (the "
                    "cell changes in the real design). Returns the first that happens, or timeout (default 60 s, at most 300; "
                    f"waiting for a job, at most {WAIT_JOB_MAX_S:g}, and a timeout says where the jobs are).",
     "input_schema": _obj({"events": {"type": "array", "items": S}, "timeout_s": {"type": "number"}}, ["events"])},
    {"name": "batch", "write": True, "fn": batch,
     "description": "Run several tool calls in your changeset as one step, all or nothing: ops [{tool, args}] in order (edits such "
                    "as add_instances, connect_nets, set_instance_params, sync_layout, add_box, write_rtl, add_intent, and reads such "
                    "as get_netlist to see the result; not open_changeset, get_changeset or the run_* tools). If one fails, none of "
                    "them is kept and the error says which. With dry_run: run them, return each result and what would change "
                    "(cell, view, effects), and keep nothing. reason is used for edits without their own. One operation in the "
                    "history (one restore point).",
     "input_schema": _obj({"ops": {"type": "array", "items": {"type": "object", "properties": {"tool": S, "args": {"type": "object"}},
                                                              "required": ["tool"]}},
                           "dry_run": {"type": "boolean", "description": "run, report, and keep nothing"}, **REASON}, ["ops"])},
    {"name": "set_instance_params", "write": True, "fn": set_instance_params,
     "description": "Set parameters of a schematic instance in your changeset, e.g. {\"W\": \"1.5\"}. Values are strings as in the schematic.",
     "input_schema": _obj({**LIBCELL, "instance": S, "params": {"type": "object", "additionalProperties": {"type": "string"}}, **REASON},
                          ["lib", "cell", "instance", "params", "reason"])},
    {"name": "add_instance", "write": True, "fn": add_instance,
     "description": "Place a symbol (e.g. sky130_fd_pr/nfet_01v8.sym, devices/res.sym) in a schematic of your changeset; without x / y it goes to free space next to the drawing, so nothing drawn moves. rot turns it by 90 degree steps, flip mirrors it left to right (e.g. the right half of a differential pair, so its gate faces outward); list_symbols gives the pin offsets before rotation / mirroring.",
     "input_schema": _obj({**LIBCELL, "symbol": S, "x": {"type": "number"}, "y": {"type": "number"}, "rot": {"type": "integer"}, "flip": {"type": "boolean"},
                           "params": {"type": "object", "additionalProperties": {"type": "string"}}, **REASON},
                          ["lib", "cell", "symbol", "reason"])},
    {"name": "get_netlist", "write": False, "fn": get_netlist,
     "description": "The cell's netlist: its connectivity without the drawing (ports in order, each instance's symbol, parameters and pin -> net, supply_pins a symbol takes as parameters (standard cells' VPWR / VGND / VPB / VNB -> net), the code blocks' text such as a testbench's .meas / .control / .options, and pending connections: in the netlist, not drawn yet), with warnings first, such as a supply parameter naming no net of the schematic. Read this to know what is connected; the drawing is only how it is shown (get_drawing). A large cell a page at a time (offset, limit), or only the instances named or those on some nets (with each net's pins). A testbench's long code: code false (only the blocks' names) or code_limit.",
     "input_schema": _obj({**LIBCELL, "instances": {"type": "array", "items": S, "description": "only these instances (or code blocks)"},
                           "nets": {"type": "array", "items": S, "description": "only the instances on these nets (by a pin or a supply parameter), each net's pins (supply parameters as inst:VPWR), and the code blocks naming them"},
                           "offset": {"type": "integer", "description": "instances to skip (paging; the answer gives next_offset)"},
                           "limit": {"type": "integer", "description": "instances to list"},
                           "all_params": {"type": "boolean", "description": "also the parameters left at their symbol's formula defaults (sky130's ad, as, pd, ps ...)"},
                           "code": {"type": "boolean", "description": "the code blocks' text (default true; false: only their names)"},
                           "code_limit": {"type": "integer", "description": "at most this many characters of each code block's text"}},
                          ["lib", "cell"])},
    {"name": "tidy_schematic", "write": True, "fn": tidy_schematic,
     "description": "Re-place the symbols tools placed that have nothing drawn to them, in free space next to the drawing. Never moves what a person placed or moved, or anything with wires.",
     "input_schema": _obj({**LIBCELL, **REASON}, ["lib", "cell", "reason"])},
    {"name": "add_wire", "write": True, "fn": add_wire, "description": "Draw a wire segment (schematic units, 10 per grid step).",
     "input_schema": _obj({**LIBCELL, **{k: {"type": "number"} for k in ("x1", "y1", "x2", "y2")}, "label": S, **REASON},
                          ["lib", "cell", "x1", "y1", "x2", "y2", "reason"])},
    {"name": "connect_pins", "write": True, "fn": connect_pins,
     "description": "Connect device pins in a schematic of your changeset by net name, pins like [\"M1:D\", \"M3:D\"] (no coordinates needed). style auto (default) draws routed wires between the pins, as a person would, and uses net labels for supplies / ground (vdd, vss, gnd ...) and nets with more than 8 pins; a pin with no safe route also gets a label. wire / label force one or the other. Wires avoid device bodies, so place the devices first with room between them (signal flowing left to right, supplies at the top, ground at the bottom). To make the net a port of the cell, also place an ipin / opin / iopin symbol (add_instance devices/ipin.sym with params {lab: <net>}) near where the net is: the same name joins it. Nothing is changed if the pins could not all be put on the net or another net would be joined. A pin drawn to another net is refused unless move is set: then it is taken off that net first (disconnect_pins). Wires stay on the 10-unit grid, and bends keep a grid step clear of other nets' pins and wires so nothing reads as connected.",
     "input_schema": _obj({**LIBCELL, "net": S, "pins": {"type": "array", "items": S}, **STYLE, **MOVE, **REASON}, ["lib", "cell", "net", "pins", "reason"])},
    {"name": "add_instances", "write": True, "fn": add_instances,
     "description": "Place several symbols in a schematic of your changeset in one step: instances [{symbol, x, y, rot?, flip?, params?}]. Same as add_instance for each.",
     "input_schema": _obj({**LIBCELL, "instances": {"type": "array", "items": {"type": "object", "properties": {
         "symbol": S, "x": {"type": "number"}, "y": {"type": "number"}, "rot": {"type": "integer"}, "flip": {"type": "boolean"}, "params": {"type": "object"}},
         "required": ["symbol"]}}, **REASON}, ["lib", "cell", "instances", "reason"])},
    {"name": "connect_nets", "write": True, "fn": connect_nets,
     "description": "Connect many nets in one step: nets {name: [\"M1:D\", \"M3:D\"], ...}, in that order (connect short local nets first, supplies last). Same as connect_pins for each net (move too); all or nothing.",
     "input_schema": _obj({**LIBCELL, "nets": {"type": "object", "additionalProperties": {"type": "array", "items": S}}, **STYLE, **MOVE, **REASON},
                          ["lib", "cell", "nets", "reason"])},
    {"name": "delete_objects", "write": True, "fn": delete_objects, "description": "Delete instances / wires (by id; an instance also by its name alone, a wire by its id 'x1,y1,x2,y2' from get_drawing) from a schematic of your changeset.",
     "input_schema": _obj({**LIBCELL, "objects": {"type": "array", "items": {"anyOf": [{"type": "string", "description": "an instance name, or a wire id 'x1,y1,x2,y2' (get_drawing)"}, {"type": "object", "properties": {"kind": {"type": "string", "enum": ["instance", "wire"]}, "id": S}, "required": ["kind", "id"], "additionalProperties": False}]}}, **REASON},
                          ["lib", "cell", "objects", "reason"])},
    {"name": "get_drawing", "write": False, "fn": get_drawing,
     "description": "How a schematic is drawn, to rearrange it: each instance's position, rotation, mirroring and extent (its "
                    "graphics with its texts, e.g. a long source value), each net's wire ids, labels and pins (netlist names), "
                    "and the instances whose extents overlap. net, instances or region narrows it down (a large drawing is "
                    "told in less detail). Wire ids ('x1,y1,x2,y2', ends sorted) go to delete_objects and label_net; move "
                    "symbols with move_instance (view schematic).",
     "input_schema": _obj({**LIBCELL, "net": S, "instances": {"type": "array", "items": S},
                           "region": {"type": "array", "items": {"type": "number"},
                                      "description": "[x1, y1, x2, y2]: what meets this part of the sheet"}}, ["lib", "cell"])},
    {"name": "disconnect_pins", "write": True, "fn": disconnect_pins,
     "description": "Take device pins (\"x5:X\") off the nets they are on in a schematic of your changeset: the wires from each "
                    "pin up to the first junction or other pin and labels sitting on it are removed, and the rest of its net "
                    "stays connected (re-routed where taking the pin off cut it apart); a pending connection is removed. Then "
                    "connect the pins anew, e.g. to insert a stage into a net. connect_pins / connect_nets with move do both.",
     "input_schema": _obj({**LIBCELL, "pins": {"type": "array", "items": S}, **REASON}, ["lib", "cell", "pins", "reason"])},
    {"name": "label_net", "write": True, "fn": label_net,
     "description": "Replace drawn wires of a net by net labels, the circuit unchanged: wires (ids from get_drawing; default all "
                    "of the net's wires) are removed with what is left dangling, and each piece of the net that no longer "
                    "carries its name gets a label (lab_pin) on one of its pins. For a wire across the sheet.",
     "input_schema": _obj({**LIBCELL, "net": S, "wires": {"type": "array", "items": S}, **REASON}, ["lib", "cell", "net", "reason"])},
    {"name": "set_port_order", "write": True, "fn": set_port_order,
     "description": "Set the order of a cell's ports (the subcircuit's pin order) in its schematic: order names every port once. "
                    "The symbol's pins take the same order when the cell has one (symbol: false leaves it); nothing is "
                    "added, removed, redrawn or rewired (sync_symbol does that). Fixes pin_order_mismatch of check_symbol.",
     "input_schema": _obj({**LIBCELL, "order": {"type": "array", "items": S},
                           "symbol": {"type": "boolean", "description": "reorder the symbol's pins too (default true)"}, **REASON},
                          ["lib", "cell", "order", "reason"])},
    {"name": "check_symbol", "write": False, "fn": check_symbol,
     "description": "Compare a cell's symbol with its schematic's ports: missing, extra or duplicate pins, directions, pin "
                    "order (pin_order_mismatch: the subcircuit's order is the schematic's), and pin sides against the cell "
                    "intent's port sides; says what sync_symbol would change.",
     "input_schema": _obj(LIBCELL, ["lib", "cell"])},
    {"name": "sync_symbol", "write": True, "fn": sync_symbol,
     "description": "Make a cell's symbol follow its schematic's ports in your changeset (creates it if missing): pin order, "
                    "directions, missing pins added and extra ones removed, and pin sides (sides {port: N|S|E|W}, else the cell "
                    "intent's port sides (set_port_intent), else as they are). A hand-drawn symbol keeps its drawing (new or "
                    "moved pins go to a free place on their side, with a lead and name); a plain box symbol is redrawn at the "
                    "size its pins need, as is any symbol with regenerate. Schematics that place it keep their connections "
                    "(the wiring of a moved pin follows it, listed as affected); a pin a consumer connects (drawn or pending) "
                    "cannot be removed.",
     "input_schema": _obj({**LIBCELL, "sides": {"type": "object", "additionalProperties": {"type": "string", "enum": list(xschem.SIDES)}},
                           "regenerate": {"type": "boolean", "description": "redraw as a box with pins (drops hand-drawn graphics)"},
                           **REASON}, ["lib", "cell", "reason"])},
    {"name": "add_via", "write": True, "fn": add_via,
     "description": "Draw a rule-correct via stack at x, y (um) in your changeset's layout, from from_layer to to_layer "
                    "(either way up; sky130: diff, tap or poly, li1, met1 ... met5; GF180MCU: comp or poly, metal1 ...; "
                    "IHP: diff or poly, metal1 ... topmetal2): each cut an array (rows x cols at every "
                    "level, or as many as fit in size [w, h]; default one), each conductor a pad with the PDK's enclosures "
                    "(the larger 'one of two adjacent sides' value on two opposite sides, along direction h or v: default "
                    "along the shape it lands on), minimum width and area, and sky130's npc around a poly contact (bridged "
                    "to npc closer than 0.27 um, as npc.2 wants: the fills in bridged; refused where no fill closes the "
                    "gap). A "
                    "contact on a diffusion or tap needs that there (not drawn). Refused, saying what and where, when it "
                    "would join two nets (with net: touch a net not called net) or come closer than a spacing to another "
                    "shape, or put a cut partly over or against a cut already there (of any net: one shape, no cut; exactly "
                    "on it is the same cut), or when Magic would refuse it next to a placed cell's own vias (abutting or partly over a via of "
                    "its kind there, the cuts of an array one contact to Magic: the same array exactly, or clear of it; any via on a MIM capacitor's own contacts, e.g. a via4 on the top plate over its "
                    "via3 array: lead the plate out past the capm first). dry_run: what it would draw, drawing nothing.",
     "input_schema": _obj({**LIBCELL, "x": NUM, "y": NUM, "from_layer": S, "to_layer": S,
                           "rows": {"type": "integer"}, "cols": {"type": "integer"},
                           "size": {"type": "array", "items": NUM, "description": "[w, h] um: fill this footprint with cuts"},
                           "direction": {"type": "string", "enum": ["h", "v"]}, "net": S,
                           "dry_run": {"type": "boolean"}, **REASON}, ["lib", "cell", "x", "y", "from_layer", "to_layer", "reason"])},
    {"name": "route", "write": True, "fn": route,
     "description": "Draw a wire on one layer in your changeset's layout between two ends: a point [x, y] (um: on the "
                    "shape there of that layer, else of the nearest layer below it, else free), a device terminal "
                    "'XMS:S' / 'XCB:c0' / 'child@x,y:D' (as get_device_geometry names them) or a net by its "
                    "label or schematic net name ('vin': its nearest shape). An end with no shape on the layer gets a "
                    "rule-correct via stack from its nearest layer. The way is the first of straight, the two L's and the "
                    "Z's that touches no shape of another net (from a connectivity extraction of the layout: labels, and "
                    "the schematic nets of the bound devices' terminals) and keeps the spacings (the wide-metal one where "
                    "either side is wide; poly never over a diffusion or tap), else one round what was "
                    "in the way (never past an end and back); through [[x, y], ...] leads it through points instead (each "
                    "leg horizontal first). A terminal or net end is left at its point nearest the other end (or the "
                    "first / last through point), inside its shape by half the wire or cut (a narrower strip: its centre "
                    "line). Every point and bend is on the 5 nm grid. A via's pads lie along the way it leaves, else turned "
                    "a quarter when only that fits (vias[].pads: along the strip it lands on, or across the way it comes "
                    "in). "
                    "Refused when the ends are on different schematic nets (or not on "
                    "net), one net already (a net in pieces, or a terminal whose strips are not joined yet: from a "
                    "piece, a strip, the other end is not on; two ends on the same few nets: from one to the others; ends "
                    "only an n-well joins are not one net: it draws the metal, note says so), or no way is clear, "
                    "saying what is in the way (also a via Magic refuses next to a placed cell's own vias, as add_via). width: default the layer's minimum. dry_run: what "
                    "it would draw, drawing nothing.",
     "input_schema": _obj({**LIBCELL, "from": END, "to": END, "layer": S, "width": NUM, "net": S,
                           "through": {"type": "array", "items": {"type": "array", "items": NUM}},
                           "dry_run": {"type": "boolean"}, **REASON}, ["lib", "cell", "from", "to", "layer", "reason"])},
    {"name": "add_tap", "write": True, "fn": add_tap,
     "description": "Draw a rule-correct substrate (kind ptap: psdm) or n-well (ntap: nsdm and the n-well around it) tap in "
                    "your changeset's layout: box [x1, y1, x2, y2] (um) is the tap, or x, y the smallest one (one contact); "
                    "contacts along it, li1 over them and, with metal met1 (the default; li1: none; or a metal above), the "
                    "vias and straps up to that metal to route it. Refused when it would touch a diffusion or another net "
                    "(also through its n-well; net: the net it is for), come closer than a tap spacing, put its implant over "
                    "the other one or near one of its kind, or its n-well near another n-well or an n+ diffusion. dry_run: "
                    "what it would draw.",
     "input_schema": _obj({**LIBCELL, "kind": {"type": "string", "enum": ["ptap", "ntap"]}, "net": S,
                           "box": {"type": "array", "items": NUM}, "x": NUM, "y": NUM, "metal": S,
                           "dry_run": {"type": "boolean"}, **REASON}, ["lib", "cell", "kind", "reason"])},
    {"name": "guard_ring", "write": True, "fn": guard_ring,
     "description": "Draw a closed guard ring of substrate (kind ptap) or n-well (ntap) taps in your changeset's layout "
                    "around instances (['XM1', 'XM2']: the extent of every piece bound to them, or ids, plus spacing, default 0.505 um on sky130, so that its "
                    "implant keeps 0.38 um from the devices' own) or a box ([x1, y1, x2, y2] um: the ring starts at its "
                    "edge): the tap ring width wide (default the narrowest that holds a row of each via up to metal; "
                    "narrower is refused), its implant, contacts, li1 and a met1 ring (metal li1: none above li1; a metal "
                    "above met1: the vias and rings up to it). An ntap ring's n-well covers what it encloses, so it "
                    "encloses pMOS only. Refused when it would touch another net (also through its n-well; net: the "
                    "ring's) or a diffusion, or break a spacing (implants, n-wells). dry_run: what it would draw.",
     "input_schema": _obj({**LIBCELL, "kind": {"type": "string", "enum": ["ptap", "ntap"]}, "net": S,
                           "around": {"anyOf": [{"type": "array", "items": S}, {"type": "array", "items": NUM}]},
                           "spacing": NUM, "width": NUM, "metal": S,
                           "dry_run": {"type": "boolean"}, **REASON}, ["lib", "cell", "kind", "around", "reason"])},
    {"name": "sync_layout", "write": True, "fn": sync_layout,
     "description": "Make the layout devices match the schematic, creating the cell's layout view when it has none: a device "
                    "whose schematic parameters changed gets the matching PCell in place (same position), missing devices "
                    "are placed at the PDK's spacing (PDK standard cells such as sky130_fd_sc_hd__inv_1 from the PDK's GDS, "
                    "a cell of your own from its layout in the library). MF / m of a MIM capacitor or resistor: that many "
                    "units, as one array bound to it (rule pitch: sky130 MIMs 1.34 um from a capm to the next plate, "
                    "capm.11 / capm.2b); a MOSFET's mult: more fingers of one strapped device. Taps: a MOSFET gets a butted "
                    "bulk tie only where a source / drain is on its bulk net (strapped to the tap: taps in the answer says "
                    "which terminal to route there), else a guard ring; thick-oxide ties are two contacts wide (0.99 um "
                    "nfet, 0.86 um pfet; diff/tap.16 0.7 um). A device put in place keeps its position and orientation. "
                    "layout: per device, layout parameters kept with its layout instance, so a later sync keeps them "
                    "(null takes one back): MOSFETs nf (fingers drawn: W x mult / nf each, the schematic keeps its nf), bulk "
                    "(auto | tie | ring | none), tap_side (right | left), gate (bottom | top | alternating), sd_contacts; "
                    "MIM / resistor units: columns. A device placed with place_pcell / place_array, in several pieces, an "
                    "array of another cell or a cell of your own bound to a device is left as it is (own_layout). notes: "
                    "what is not generated (an unsupported device's MF). Routing is not redone.",
     "input_schema": _obj({**LIBCELL, "instances": {"type": "array", "items": S, "description": "only these devices (default all)"},
                           "layout": {"type": "object", "additionalProperties": {"type": "object"},
                                      "description": "{device: {parameter: value}}, e.g. {'XMS': {'nf': 4}, 'XM5': {'bulk': 'ring'}}"},
                           **REASON}, ["lib", "cell", "reason"])},
    {"name": "create_library", "write": True, "fn": create_library,
     "description": "Create a library in the project, in your changeset (for a block of its own, e.g. the cells a generator writes). Then create_cell / import_layout into it.",
     "input_schema": _obj({"name": S, **REASON}, ["name", "reason"])},
    {"name": "place_cell", "write": True, "fn": place_cell,
     "description": "Place a layout cell in cell's layout at x, y (um: where the child's origin goes): a cell of lib, of another library child_lib (copied in with the cells under it), or a PDK standard cell by name (sky130_fd_sc_hd__nand2_1, from the PDK's GDS). rot 0/90/180/270 degrees, mirror flips about the x axis first. columns / rows with pitch_x / pitch_y (um) make one regular array instance (e.g. CDAC unit capacitors; at most 1048576 elements). name binds it to a schematic instance, as sync_layout binds devices (an array or a cell that is not the device's PCell is then your own layout: sync_layout never replaces it); without it the instance is unbound. The answer warns about the binding, and when cells under a copied child were renamed (lib has others of those names).",
     "input_schema": _obj({**LIBCELL, "child": S, "child_lib": S, "x": {"type": "number"}, "y": {"type": "number"},
                           "rot": {"type": "integer", "enum": list(ROTATIONS)}, "mirror": {"type": "boolean"}, "name": S,
                           "columns": {"type": "integer"}, "rows": {"type": "integer"},
                           "pitch_x": {"type": "number"}, "pitch_y": {"type": "number"}, **REASON},
                          ["lib", "cell", "child", "x", "y", "reason"])},
    {"name": "import_layout", "write": True, "fn": import_layout,
     "description": "Bring the cells of a GDS / OASIS file into a library's layout in your changeset: path (a file a generator wrote, under the server's import directories) or job (a run_librelane run: its final GDS). New cells are added, cells the library has the same stay; a cell it has with other content is refused (the error names them) unless listed in replace. Then place the top cell with place_cell.",
     "input_schema": _obj({"lib": S, "path": S, "job": S,
                           "replace": {"anyOf": [{"type": "array", "items": S}, {"type": "boolean"}],
                                       "description": "cells to overwrite with the file's (they exist with other content), or true: all of them"},
                           **REASON}, ["lib", "reason"])},
    {"name": "add_box", "write": True, "fn": add_box,
     "description": "Draw a rectangle on a layer ('met1', 'li1', 'poly' or 'layer/datatype' like 68/20) in your changeset's layout. Coordinates in um (snapped to 5 nm). Respect the sky130 rules (met1 width >= 0.14 um, spacing >= 0.14 um).",
     "input_schema": _obj({**LIBCELL, "layer": S, "box": {"type": "array", "items": {"type": "number"}}, **REASON}, ["lib", "cell", "layer", "box", "reason"])},
    {"name": "add_path", "write": True, "fn": add_path,
     "description": "Draw a wire (path with width) on a layer. points [[x,y],...] and width in um.",
     "input_schema": _obj({**LIBCELL, "layer": S, "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}}, "width": {"type": "number"}, **REASON},
                          ["lib", "cell", "layer", "points", "width", "reason"])},
    {"name": "add_label", "write": True, "fn": add_label,
     "description": "Put a pin / net label on a label layer ('met1.label' = 68/5, 'li1.label' = 67/5) at x, y (um); LVS uses these names.",
     "input_schema": _obj({**LIBCELL, "layer": S, "text": S, "x": {"type": "number"}, "y": {"type": "number"}, **REASON}, ["lib", "cell", "layer", "text", "x", "y", "reason"])},
    {"name": "move_instance", "write": True, "fn": move_instance,
     "description": "Move a layout instance: instance is the schematic instance it is bound to (all its pieces with dx, dy) "
                    "or its id from get_layout_info (child@x,y; also an unbound one). dx, dy move it by that much (um); x, y "
                    "put its origin there (um, as place_cell's); rot (0/90/180/270) and mirror (about the x axis, first) set "
                    "its orientation (the one not given stays), turning it about the centre of its box and then moving it "
                    "by dx, dy unless x, y are given (rot 180 with mirror: mirrored left to right); an array's steps turn "
                    "with it. Attached wires do not follow. With view schematic: move schematic symbols "
                    "(instance, or instances moved together) by dx, dy or the first to x, y (schematic units, snapped to "
                    "the 10-unit grid); the wires attached to their pins are re-routed, and nothing moves when that is not "
                    "possible.",
     "input_schema": _obj({**LIBCELL, "instance": S, "dx": {"type": "number"}, "dy": {"type": "number"},
                           "view": {"type": "string", "enum": ["layout", "schematic"], "description": "layout (default) or schematic"},
                           "instances": {"type": "array", "items": S, "description": "schematic: several symbols moved together"},
                           "x": {"type": "number"}, "y": {"type": "number"},
                           "rot": {"type": "integer", "enum": list(ROTATIONS), "description": "layout: the orientation, degrees counter-clockwise"},
                           "mirror": {"type": "boolean", "description": "layout: mirrored about the x axis (before rot)"},
                           **REASON}, ["lib", "cell", "reason"])},
    {"name": "swap_instances", "write": True, "fn": swap_instances,
     "description": "Exchange two layout instances in one recorded operation (one restore point): instances [a, b], each "
                    "an id from get_layout_info (child@x,y) or the schematic instance it is bound to (a device in one "
                    "piece). what place (default): each takes the other's origin and orientation (two units of a CDAC "
                    "swapped between bits, no free spot needed in between; an array's steps turn with it); what binding: "
                    "each stays where it is and takes the other's binding (the schematic instance, its layout "
                    "parameters), only between instances of one cell. Ids name places, so after a place swap each id "
                    "names the other instance. Attached wires do not follow.",
     "input_schema": _obj({**LIBCELL, "instances": {"type": "array", "items": S, "minItems": 2, "maxItems": 2,
                                                    "description": "the two instances: ids (child@x,y) or bound schematic instance names"},
                           "what": {"type": "string", "enum": ["place", "binding"],
                                    "description": "place (default): exchange origins and orientations; binding: exchange what they are bound to"},
                           **REASON}, ["lib", "cell", "instances", "reason"])},
    {"name": "delete_instances", "write": True, "fn": delete_instances,
     "description": "Delete layout instances (devices, units, dummies, cells placed) from a cell of your changeset: instances "
                    "(ids from get_layout_info, child@x,y, or schematic instance names: every piece bound to it), and / or "
                    "box [x1, y1, x2, y2] (um: those lying inside it), and / or child (instances of that cell); each one "
                    "deleted meets all the criteria given. The wiring to them stays; a deleted bound device is missing "
                    "until sync_layout places it again.",
     "input_schema": _obj({**LIBCELL, "instances": {"type": "array", "items": S}, "box": {"type": "array", "items": {"type": "number"}},
                           "child": S, **REASON}, ["lib", "cell", "reason"])},
    {"name": "delete_unused_cells", "write": True, "fn": delete_unused_cells,
     "description": "Take the cells Undine generated for PDK PCells (place_pcell's and sync_layout's variants, e.g. "
                    "sky130_fd_pr__nfet_01v8_w4p0_l0p15_730e84, and sync_layout's _par / _tie cells round them) that nothing "
                    "places any more out of a library of your changeset: those a sync_layout that changed nf / the gate side "
                    "or a deleted place_pcell piece left behind. Without cells: those this changeset created (the answer "
                    "counts the baseline's, left by changesets applied before, in baseline_unused; include_baseline: true "
                    "takes them too); dry_run lists them first. With cells: only those, each refused when it is not such "
                    "a cell or is still placed. Recorded with each cell's content: restore_changeset brings them back. "
                    "run_checks skips such cells anyway. A cell of your own (a pilot) goes with delete_cell.",
     "input_schema": _obj({"lib": {"type": "string", "description": "library name"},
                           "cells": {"type": "array", "items": S, "description": "only these cells (default: the unused generated ones this changeset created)"},
                           "include_baseline": {"type": "boolean", "description": "without cells: the baseline's unused generated cells too"},
                           "dry_run": {"type": "boolean", "description": "list what would go, delete nothing"}, **REASON}, ["lib"])},
    {"name": "delete_cell", "write": True, "fn": delete_cell,
     "description": "Delete a cell from a library of your changeset: its layout, schematic and / or symbol (views; default "
                    "every one it has), e.g. a pilot cell you emptied. Refused while anything places it: its layout in "
                    "another cell's layout (delete_instances first), its symbol in a schematic or testbench, its "
                    "schematic while the symbol stays and is placed. Recorded with the content (shapes and instances, the "
                    "files' text): restore_changeset to a point before brings it back; applying the changeset removes it "
                    "from the project. The cells its layout placed stay (delete_unused_cells takes generated ones); "
                    "dry_run says what would go. The answer says which views this changeset itself had created.",
     "input_schema": _obj({**LIBCELL, "views": {"type": "array", "items": {"type": "string", "enum": list(DELETABLE_VIEWS)},
                                                "description": "the views to delete (default: every one the cell has)"},
                           "dry_run": {"type": "boolean", "description": "check and say what would go, delete nothing"},
                           **REASON}, ["lib", "cell"])},
    {"name": "place_pcell", "write": True, "fn": place_pcell,
     "description": "Generate a PDK PCell with the parameters you choose and place it (its origin at x, y, um; rot, mirror as "
                    "place_cell): a dummy (unbound), or with name a piece of that schematic instance (a device split into "
                    "several, which LVS combines once routed in parallel; sync_layout then leaves the device as your own "
                    "layout, one piece too). pcell and params as the PDK's catalog names them (sky130: nfet / pfet {type, w (one "
                    "finger), l, nf, bulk: None | bulk tie | guard ring, gate_con_pos, sd_con_col}, mim_cap {type, w, l}, "
                    "res_poly ...): an unknown one is refused with the PCells, or the parameters with their defaults and "
                    "choices. A variant the library has is not generated again. The answer gives its id and box.",
     "input_schema": _obj({**LIBCELL, "pcell": S, "params": {"type": "object"}, "x": {"type": "number"}, "y": {"type": "number"},
                           "rot": {"type": "integer", "enum": list(ROTATIONS)}, "mirror": {"type": "boolean"},
                           "name": {"type": "string", "description": "the schematic instance this piece belongs to (default: unbound)"},
                           **REASON}, ["lib", "cell", "pcell", "x", "y", "reason"])},
    {"name": "place_array", "write": True, "fn": place_array,
     "description": "Place the units of several schematic devices in one grid, each unit an instance bound to its device "
                    "(LVS combines them once routed): MIM capacitors and resistors by their MF x m, MOSFETs as one-finger "
                    "units of W x mult / units (devices {name: units}; default their nf x mult, or layout nf) without a "
                    "tap (put taps or a guard ring around). pattern: common_centroid (each device's units in pairs "
                    "point-symmetric about the centre, spread from the middle out by their share: a binary CDAC's units, "
                    "a matched pair), interdigitated (dealt in turn, ABAB, each row reversed), or rows (rows: the grid "
                    "cell by cell, top row first, device names, 'dummy', '' free). columns (default: as square as the "
                    "count allows), pitch [x, y] um (default the PDK's rule pitch: sky130 1.17 um MIM units 4.17 x 2.71), "
                    "origin [x, y] um the lower-left corner, dummies: rings of dummy units around (unbound, or bound to "
                    "the schematic instance dummy, a device of their own with exactly that many units, MF: else refused). "
                    "A device placed already is refused unless replace. Answers the unit map (one character per unit, "
                    "legend), each device's units and centroid offset, pitch, box and unit_origin_um (a unit's id: its "
                    "lower-left corner plus that); sync_layout leaves the pieces as they are.",
     "input_schema": _obj({**LIBCELL, "devices": {"anyOf": [{"type": "array", "items": S},
                                                            {"type": "object", "additionalProperties": {"type": "integer"}}],
                                                  "description": "schematic instances, e.g. ['C0', 'C1', 'CDUM'], or {name: units}"},
                           "pattern": {"type": "string", "enum": ["common_centroid", "interdigitated", "rows"]},
                           "rows": {"type": "array", "items": {"anyOf": [{"type": "array", "items": S}, S]},
                                    "description": "pattern rows: top row first, each a list of device names, 'dummy' or ''"},
                           "columns": {"type": "integer"}, "pitch": {"type": "array", "items": {"type": "number"}},
                           "origin": {"type": "array", "items": {"type": "number"}},
                           "dummies": {"type": "integer", "description": "rings of dummy units around the array"},
                           "dummy": {"type": "string", "description": "the schematic instance the dummy units are bound to: one of its own, "
                                                                 "with as many units (MF) as there are dummies"},
                           "replace": {"type": "boolean", "description": "delete the devices' instances placed before"},
                           **REASON}, ["lib", "cell", "origin", "reason"])},
    {"name": "delete_shapes", "write": True, "fn": delete_shapes,
     "description": "Delete the cell's own shapes lying completely inside a box (um), optionally only on one layer (max 40 per call).",
     "input_schema": _obj({**LIBCELL, "box": {"type": "array", "items": {"type": "number"}}, "layer": S, **REASON}, ["lib", "cell", "box", "reason"])},
    {"name": "run_checks", "write": True, "fn": run_checks,
     "description": "Run DRC / LVS on the cells your changeset changed, on both the baseline and your working copy, and wait for the results; each schematic it changed is checked too (kind schematic: symbol against its ports, supply parameters naming no net, pending connections). DRC runs the PDK's KLayout deck and, where the PDK has a Magic tech (sky130, GF180, IHP), Magic's drc(full) (checker magic); with LVS comes an ERC row (kind erc, warnings: a body tap butted against a source / drain of another net, by instance, terminal and both nets). Each row names its job, when it finished, and reused: true when it is a job that had already run on the same inputs; fresh: true runs every check anew. Use it after editing; get_drc_report shows where the violations are.",
     "input_schema": _obj({"fresh": {"type": "boolean", "description": "run every check anew instead of reusing jobs that "
                                     "ran on the same inputs (default false)"}})},
    {"name": "get_intent", "write": False, "fn": get_intent,
     "description": "The cell's design intent (in your working copy once a changeset is open), on two levels. The cell intent says what the cell has to achieve: notes, ports (kind, side), supplies, and items of kind goal / preference / constraint (strength must or should) / spec / context, each with why and source. views holds view intent: how one view (schematic, symbol, rtl, layout, test:<testbench cell>) realises it, each item linked with serves to the cell items it comes from. Plus problems against the schematic and what each flow step is missing. A summary is already in your design context. With ids / kinds: only those items (cell and view intent), and the problems about them.",
     "input_schema": _obj({**LIBCELL, "ids": {"type": "array", "items": S, "description": "only these items, e.g. ['s1', 'c3']"},
                           "kinds": {"type": "array", "items": {"type": "string", "enum": list(intent.KINDS)}, "description": "only items of these kinds"}},
                          ["lib", "cell"])},
    {"name": "add_intent", "write": True, "fn": add_intent,
     "description": "Add an item to the cell's design intent in your changeset; with view (schematic, layout, rtl, symbol or test:<testbench cell>) to that view's intent, linked with serves to the cell items it realises (requirements belong to the cell intent, how a view meets them to the view). Use it for what should outlast the task: a requirement or priority the user states (pass request with their words), a goal you were given, a constraint a person decided. kind: goal (direction to optimise), preference (how to trade off), constraint (to respect; strength must, the default, or should; rule e.g. {type: symmetric|match|place, instances: [M1, M2]} or {type: net, net: tail, min_width_um: 0.5}), spec (spec {measure: dc_gain_db, min: 60, testbench: tb_ac, output: out, analysis: {type: ac, fstop: 1G}}; output / analysis only for AC figures; meas: '.meas tran tphl TRIG v(in) VAL=0.9 RISE=1 TARG v(out) VAL=0.9 FALL=1' measures a delay or the like with measure tphl, without changing the testbench; for an ADC or a sample-and-hold, measure sndr_db / snr_db / thd_db / sfdr_db / enob / signal_dbfs with spectrum {bits: [d0, ..., d7] (LSB first) or signal: out, fs, points, start, full_scale?} and a tran analysis, as in run_simulation; measure may be settle(v(out), tol=0.5m, from=10n) or derived, e.g. max(t1, t2); corners / temps: where it is checked when check_specs is not told, a list or a mapping to the limits that differ there, e.g. corners {tt: {}, ss: {max: 97e-9}}; conditions: its own test conditions, as check_specs takes; substitute: {lib/cell: lib/cell}, a stand-in of the same pins netlisted for every instance of a cell in its runs, e.g. {'adc/sar_logic': 'adc/sar_logic_ideal'} for an SNDR from a long transient; missing: fail when a run whose .meas gives no value (it never triggered, e.g. a control that never clears) does not meet the spec, instead of leaving it not measured; check_specs measures it), context (background). In a testbench's own intent, a constraint with rule {type: substitute, cells: {lib/cell: lib/cell}} gives the stand-ins all its runs use by default. Not for one-off directions such as 'try W = 2'.",
     "input_schema": _obj({**LIBCELL, "kind": {"type": "string", "enum": list(intent.KINDS)}, "text": S,
                           "strength": {"type": "string", "enum": list(intent.STRENGTHS)}, "rule": {"type": "object"},
                           "spec": {"type": "object"}, "serves": {"type": "array", "items": S},
                           "view": {"type": "string", "description": "the view whose intent gets the item; omit for the cell intent"},
                           "why": {"type": "string", "description": "why the designer wants this (kept with the item)"},
                           "request": {"type": "string", "description": "the user's words this item comes from, quoted"},
                           **REASON}, ["lib", "cell", "kind", "text", "reason"])},
    {"name": "update_intent", "write": True, "fn": update_intent,
     "description": "Change an intent item (by id) in your changeset: fields to set, e.g. {spec: {...}} or {strength: should}; "
                    "an empty value clears one. fields.request: the user's words that later confirm or extend the item, "
                    "quoted; they are added to its request history (source.requests, with the date) after the words it came "
                    "from, never in their place. Changing or relaxing what a person set is a proposal they review: say so in your reply.",
     "input_schema": _obj({**LIBCELL, "id": S, "fields": {"type": "object", "description":
                           "e.g. {text, strength, rule, spec, serves, why} to set, and request (the user's words) to add"},
                           **REASON}, ["lib", "cell", "id", "fields", "reason"])},
    {"name": "remove_intent", "write": True, "fn": remove_intent,
     "description": "Remove an intent item (by id) in your changeset; say in your reply why it no longer applies.",
     "input_schema": _obj({**LIBCELL, "id": S, **REASON}, ["lib", "cell", "id", "reason"])},
    {"name": "set_port_intent", "write": True, "fn": set_port_intent,
     "description": "Set a port's intent: kind (signal_in, signal_out, inout, bias_in, clock, supply, ground) and side on the cell outline (N, S, E, W, or auto to let the tool decide).",
     "input_schema": _obj({**LIBCELL, "port": S, "kind": {"type": "string", "enum": list(intent.PORT_KINDS)},
                           "side": {"type": "string", "enum": list(intent.SIDES)}, "why": S, **REASON}, ["lib", "cell", "port", "reason"])},
    {"name": "set_supply_intent", "write": True, "fn": set_supply_intent,
     "description": "Mark a net as the supply or ground in the cell's intent (role supply | ground; omit role to clear).",
     "input_schema": _obj({**LIBCELL, "net": S, "role": {"type": "string", "enum": list(intent.SUPPLY_ROLES)}, **REASON},
                          ["lib", "cell", "net", "reason"])},
    {"name": "set_intent_notes", "write": True, "fn": set_intent_notes,
     "description": "Replace the free-text notes of the cell's intent (the designer's context and priorities in prose). Keep what is there unless it is wrong.",
     "input_schema": _obj({**LIBCELL, "text": S, **REASON}, ["lib", "cell", "text", "reason"])},
    {"name": "get_rtl", "write": False, "fn": get_rtl,
     "description": "The RTL (Verilog / SystemVerilog) of a cell: its file text, the ports of its module, and the library's other RTL files (also sources of a simulation).",
     "input_schema": _obj(LIBCELL, ["lib", "cell"])},
    {"name": "write_rtl", "write": True, "fn": write_rtl,
     "description": "Write the whole RTL file of a cell in your changeset (creates it if needed). The file must define the module named like the cell; other modules in it are fine. Returns Verilator's lint of the result. A testbench is a cell too (e.g. tb_counter, with initial blocks, $display, $error / assertions and $finish; $dumpfile / $dumpvars for a waveform).",
     "input_schema": _obj({**LIBCELL, "text": S, **REASON}, ["lib", "cell", "text", "reason"])},
    {"name": "lint_rtl", "write": False, "fn": lint_rtl,
     "description": "Verilator lint (-Wall) of the library's RTL with this cell as the top module: errors and warnings with file and line. A cell drawn from standard cells (a schematic, no RTL) and the schematic cells the RTL instantiates are read as their gate-level netlists (export_verilog), with the PDK's functional Verilog models of the cells (gate_level says which).",
     "input_schema": _obj(LIBCELL, ["lib", "cell"])},
    {"name": "run_rtl_sim", "write": False, "fn": run_rtl_sim,
     "description": "Simulate a testbench cell with Verilator (--binary --timing; the library's RTL files are the sources; a cell it instantiates that has only a schematic of standard cells is its gate-level netlist, simulated with the PDK's functional cell models, zero delay). Returns what the testbench printed, its errors ($error, $fatal, failed assertions; the run fails with any), where it finished, build warnings, and the last changes of the dumped signals. Design to the cell's intent: check its specs in the testbench.",
     "input_schema": _obj(LIBCELL, ["lib", "cell"])},
    {"name": "list_flows", "write": False, "fn": list_flows,
     "description": "The project's LibreLane flow configurations (top cell, clock, floorplan, variables) and whether LibreLane is available here.",
     "input_schema": _obj({}, [])},
    {"name": "check_flow", "write": False, "fn": check_flow,
     "description": "What a flow still needs before a LibreLane run (clock, port sides in the top cell's intent ...), or, when nothing is missing, the LibreLane settings it derives with the flow / intent entries each comes from. flow: a saved flow; or config: a configuration not saved yet (check it before write_flow).",
     "input_schema": _obj({"flow": S, "config": FLOW_CONFIG}, [])},
    {"name": "write_flow", "write": True, "fn": write_flow,
     "description": "Propose a LibreLane flow configuration (flows/<flow>.yaml) in your changeset: the person reviews and applies it like any edit (recorded in the top cell's library, view flow). config: {top: {lib, cell}, source: rtl | schematic (a cell drawn from standard cells, built from its schematic as drawn: no synthesis, resizing, buffering, clock tree or antenna diodes), clock: {port, period_ns}, floorplan: {die_area_um: [x0, y0, x1, y1]} or {core_util}, variables: {LibreLane variable: value}}; delete: true takes the flow out. Returns what check_flow says of it.",
     "input_schema": _obj({"flow": S, "config": FLOW_CONFIG, "delete": {"type": "boolean"}, **REASON}, ["flow", "reason"])},
    {"name": "export_verilog", "write": False, "fn": export_verilog,
     "description": "The gate-level (structural) Verilog of a cell drawn from the PDK's standard cells: one module named like the cell, its ports in the schematic's order, every standard cell as an instance with its pins by name (power pins inside `ifdef USE_POWER_PINS). Returns the text, the cells used and warnings (an input on a supply instead of a tie cell ...); refuses an instance that is not a standard cell. Writes nothing: a flow with source schematic takes this netlist.",
     "input_schema": _obj(LIBCELL, ["lib", "cell"])},
    {"name": "run_librelane", "write": False, "fn": run_librelane,
     "description": "Start a LibreLane RTL-to-GDS run of a flow (the top cell's RTL and design intent as inputs). Returns a job id at once; the run takes minutes.",
     "input_schema": _obj({"flow": S}, ["flow"])},
    {"name": "get_librelane_result", "write": False, "fn": get_librelane_result,
     "description": "The state of a LibreLane run; when done its key metrics (cells, area, timing slack and violations, DRC / LVS / antenna counts, power), the final GDS and errors.",
     "input_schema": _obj({"job": S}, ["job"])},
    {"name": "run_simulation", "write": False, "fn": run_simulation,
     "description": "Run ngspice on a testbench schematic (in your working copy if a changeset is open). The PDK models are "
                    "included when the testbench has none (or place the PDK corner symbol to choose a corner). Returns, by "
                    "analysis: op: every node voltage and each transistor's bias (id, vgs, vds, vth, vdsat, gm, gds, region, "
                    "vds_minus_vdsat) with the devices not in saturation; ac: with output (and input): dc_gain_db, f_3db_hz, "
                    "unity_gain_freq_hz, phase_margin_deg, gain_margin_db and gain / phase per decade; tran / dc: per signal "
                    "initial, final, min, max, steepest slope and a sampled waveform; plus .meas results. With spectrum (tran "
                    "only): a coherent FFT of the run sampled as clocked, for an ADC's codes or a sample-and-hold output: "
                    "sndr_db, snr_db, thd_db, sfdr_db (and its spur), enob, signal_dbfs, harmonics. On failure: the "
                    "error lines of the log and what they usually mean. Without analysis the schematic's own .control runs. "
                    "With extracted: post-layout, those cells from their parasitic extraction (the result names them). "
                    "With substitute (or the testbench's default): a stand-in netlisted for every instance of a cell, e.g. "
                    "a behavioural SAR controller in an ADC's FFT run (the result names it). "
                    "corner / temp / options run it at a PDK corner, a temperature, with simulator options, without "
                    "editing the testbench. "
                    "Each run uses one ngspice thread and waits its turn when the server already runs its limit of "
                    "simulations. A run still going after wait_s (default 150 s) is not an error: the answer gives its job "
                    "(queued with its place in line, or running for how long); wait_for job:<id>, then run_simulation with "
                    "job for its results, or cancel_job.",
     "input_schema": _obj({**LIBCELL,
                           "analysis": {"type": "object", "description":
                                        "op: {type:'op'}; tran: {type:'tran', step, stop} e.g. step '1n', stop '2u', and "
                                        "optionally tstart (results from then on), tmax (the largest time step; or maxstep, "
                                        "as options set it) and uic: true (start from the .ic values, without an operating "
                                        "point); "
                                        "ac: {type:'ac', sweep:'dec', points, fstart, fstop} e.g. points '20', fstart '1', fstop '1G'; "
                                        "dc: {type:'dc', source, start, stop, step}. Other keys are refused. It replaces "
                                        "the testbench's own .control blocks and analysis lines for this run.",
                                        "properties": {"type": {"type": "string", "enum": ["op", "tran", "ac", "dc"]}},
                                        "required": ["type"]},
                           "signals": {"type": "array", "items": S, "description": "nodes to report, e.g. ['out', 'x1.tail'] (default: up to 24 voltages). "
                                       "A transient stores the testbench's own nets, what its .meas lines and measures "
                                       "name and these: a node inside a cell (x1.tail) is kept only when named here"},
                           "save_all": {"type": "boolean", "description": "store every node of the run, inside its cells "
                                        "too (default false: a transient stores the testbench's nets and what this call "
                                        "names). A mixed-signal transient of every node is hundreds of megabytes, and a "
                                        "changeset keeps every run: name the nodes you need in signals instead"},
                           "output": {"type": "string", "description": "ac: the output node for the gain / phase figures, e.g. 'out'"},
                           "input": {"type": "string", "description": "ac: the input node (default: the 1 V AC source)"},
                           "measure": {"type": "array", "items": S, "description":
                                       "ngspice .meas statements for this run only (the testbench is not changed), e.g. "
                                       "'.meas tran tphl TRIG v(in) VAL=0.9 RISE=1 TARG v(out) VAL=0.9 FALL=1' or "
                                       "'.meas ac g10 FIND vm(out) AT=10Meg'; their values come back in measurements. Also "
                                       "measures Undine reads off the run (in measures): a settling time "
                                       "'settle(v(out), tol=0.5m, from=10n, to?, target?)' (how long after from v(out) stays "
                                       "within tol of its value at to, the end by default, or of target), or a derived "
                                       "measure over .meas results and other measures: 'max(t1, t2)', '-iavg*1.8'"},
                           "corner": {"type": "string", "description": "a PDK model corner for this run, e.g. 'ss' (default: as the testbench is)"},
                           "temp": {"type": "number", "description": "temperature in °C for this run (default: as the testbench is)"},
                           **OPTIONS,
                           "spectrum": {"type": "object", "description":
                                        "tran: sample the run at start + k / fs, k = 0 .. points-1, and take a rectangular-window "
                                        "FFT (coherent sampling: fin = fin_bin * fs / points, fin_bin odd and prime to points). "
                                        "{signal: 'out' (or ['outp', 'outn']) | bits: ['d0', ..., 'd7'] (LSB first; one "
                                        "threshold, default midway between the lowest and highest bit level; weights default "
                                        "1, 2, 4 ...), fs: '1Meg' or period: '1u', points: 256, start: '1.5u' (the first "
                                        "sample: when the first code is valid), fin_bin? (default the largest bin), harmonics? "
                                        "(counted as distortion from the 2nd, default 5), full_scale? (peak-to-peak volts, or "
                                        "2^N codes, for signal_dbfs)}"},
                           "sample": {"type": "object", "properties": SAMPLE, "required": ["signals"], "description":
                                      "tran: signals at full precision at times [...] or at start + k / fs (k < points, "
                                      "as spectrum samples), as rows [time, value, ...]: to check a figure yourself "
                                      "(sample_waveform reads a finished run the same way)"},
                           **EXTRACTED, **SUBSTITUTE,
                           "job": {"type": "string", "description": "the results of this earlier run of the testbench (a "
                                   "job id from run_simulation) instead of a new run, with signals / output / input / "
                                   "spectrum / sample and measures read off the run (not .meas statements) as you like; "
                                   "waits for it like a new run if it is still going"},
                           "wait_s": {"type": "number", "description": f"how long to wait for the run before answering that "
                                      f"it is still going (default {WAIT_S}, 0 to answer at once, at most {WAIT_JOB_MAX_S:g})"},
                           **TIME_LIMIT},
                          ["lib", "cell"])},
    {"name": "cancel_job", "write": False, "fn": cancel_job,
     "description": "Stop a simulation (or another job) of yours that is queued or running: a job in your changeset's "
                    "working copy, or one this session started in the project. Its ngspice is terminated (then killed); "
                    "the job is marked cancelled. Stop outdated runs (the testbench changed) so they do not take the "
                    "machine others share. A job that has ended but left processes running (a changeset cannot be "
                    "closed while they run) has those stopped.",
     "input_schema": _obj({"job": S}, ["job"])},
    {"name": "extract_parasitics", "write": False, "fn": extract_parasitics,
     "description": "Extract a layout cell with its parasitics (Magic, once DRC and LVS are clean: on a layout whose LVS does not "
                    "match it extracts anyway, for diagnosis, with a warning), in your working copy if a "
                    "changeset is open: a SPICE subcircuit with the schematic's ports in its order, the devices, each net's "
                    "capacitance to the substrate and to other nets (coupling), and with mode rc the wire resistance (the c "
                    "mode's capacitances spread over the resistor nodes, coupling kept between the two nets: Magic's own rc "
                    "flow counts it twice). Returns the summary to diagnose with: capacitance per net (largest first), the "
                    "largest couplings, resistance per net (rc), devices by model, whether the ports match the schematic, "
                    "floating nets. Inner nets keep the schematic's names, and devices take the schematic's element names "
                    "(XXM5, so @m.x1.xxm5...[id] works; a device split in fingers / m: XXM5, XXM5_2 ..), when an LVS run of "
                    "the same layout matched them (names; else Magic's a_N_M# and X0 ..: run_checks first). A MIM's top plate "
                    "shields its bottom plate from the metal above (Magic's tech does not let it): the coupling of that "
                    "metal to the bottom plate inside the capacitor, by area and fringe, is not counted between the "
                    "plates (the device has it) and goes to the top plate for another net's metal (mim_shielding: what "
                    "Magic gave, what is kept, per pair of nets). runtime_s: how long it took. "
                    "c is enough for capacitive loading and coupling (a CDAC, a comparator's inputs); rc adds "
                    "the wiring's delay and IR drop (a switch's Ron with its wires) and takes longer on large layouts; "
                    "its single_node lists the nets left one node and why (below res_threshold_ohm, no port or "
                    "source / drain to start from, the substrate). Waits up to 150 s (else wait_for the "
                    "job, then get_parasitics). Then simulate with extracted; a layout change makes it outdated.",
     "input_schema": _obj({**LIBCELL, "mode": {"type": "string", "enum": ["c", "rc"], "description": "c (default): capacitance; rc: and resistance"},
                           "cthresh_ff": {"type": "number", "description": "leave out capacitors below this (fF, default 0.01)"},
                           "res_threshold_ohm": {"type": "number", "description": "rc: a net whose largest wiring "
                                                 "resistance is below this keeps one node, and smaller resistors are "
                                                 "merged where the network allows (ohm, default 0.1; Magic's own 10 "
                                                 "keeps the nets of a large digital block lumped)"},
                           "res_nets": {"type": "array", "items": S, "description": "rc: only these nets get a resistor "
                                        "network, named in any case (default: every net with a port or a source / "
                                        "drain)"},
                           "reason": {"type": "string", "description": "why (kept with the job)"}}, ["lib", "cell"])},
    {"name": "get_parasitics", "write": False, "fn": get_parasitics,
     "description": "The latest parasitic extraction of a cell (or a given job / mode) as extract_parasitics returns it, "
                    "with outdated: true / false (true: what changed since, in changed) and made_from: the layout's hash and "
                    "when it was extracted; with net: only that net's rows (its capacitance, its couplings, its resistance).",
     "input_schema": _obj({**LIBCELL, "job": S, "mode": {"type": "string", "enum": ["c", "rc"]}, "net": S}, ["lib", "cell"])},
    {"name": "check_specs", "write": False, "fn": check_specs,
     "description": "Measure the cell's specs (its design intent) on their testbenches, in your working copy if a changeset is "
                    "open, and say for each whether it is met and by what margin; with corners / temps, at every combination "
                    "(the worst case counts). A spec's measure is a .meas name of the testbench, an AC figure (dc_gain_db, "
                    "f_3db_hz, unity_gain_freq_hz, phase_margin_deg, gain_margin_db: the spec needs output, and analysis "
                    "{type: ac, ...} unless the testbench runs one), v(node) of an operating point, <device>.<quantity> "
                    "(e.g. x1.xm1.gm), <signal>.<min|max|initial|final> of a transient, a spectral figure of a transient "
                    "(sndr_db, snr_db, thd_db, sfdr_db, enob, signal_dbfs: the spec needs spectrum, as in run_simulation), or "
                    "the result of the spec's own meas (.meas statements added to its runs only: delays, rise times), a "
                    "settling time settle(v(out), tol=0.5m, from=10n), or a derived measure (max(t1, t2), -iavg*1.8). Run it "
                    "before and after a change; with extracted, post-layout (the specs on the extracted cells). With several "
                    "points, points gives each spec's value at each; a point that failed or gave no value is listed as such "
                    "(not_measured), never replaced by another's value, and the spec is then not fully measured; a spec "
                    "with missing: fail is not met where its .meas never triggered instead (no_value_at, no_value_why "
                    "\"no value ...\"; in points no_value; with mismatch no_value and no_value_runs); points that "
                    "do not fit in the answer are left out from the end (points_not_shown). runs says how many runs were "
                    "reused (finished runs on the same inputs) and when they ran; fresh: true reruns them. "
                    "A spec's own corners / temps / conditions are used when the call gives none, with its limits there; "
                    "its own substitute (stand-ins, e.g. a behavioural model of a slow block) always, under the call's. "
                    "Each spec's report names the stand-ins it was measured with (substitute).",
     "input_schema": _obj({**LIBCELL, **SPEC_ARGS}, ["lib", "cell"])},
    {"name": "sweep", "write": False, "fn": sweep,
     "description": "Explore a design space without editing: measure the cell's specs at every combination of instance property "
                    "values, netlisted as if edited, each judged by its worst case over conditions / corners / temps, and "
                    "rank them by their worst margin. "
                    "To size against specs: sweep coarse, then refine around the best point, set the chosen values with "
                    "set_instance_params in a changeset, and confirm with check_specs (corners: ['all']). At most "
                    f"{specsmod.MAX_JOBS} simulations per call; runs already done are reused.",
     "input_schema": _obj({**LIBCELL, "vary": {"type": "object", "description":
                           "the design choices: instance.property (in the cell) or cell/instance.property -> a list of values, "
                           "or {from, to, steps, scale: lin|log}; e.g. {'M1.W': [1, 2, 4], 'M3.L': {from: 0.15, to: 1, "
                           "steps: 4, scale: log}}; options.<name> sweeps a simulator option (reltol, maxstep ...)"},
                           **SPEC_ARGS}, ["lib", "cell", "vary"])},
]
BY_NAME = {t["name"]: t for t in TOOLS}

# The tools that read and check another agent's open changeset with `changeset` (call: _reader), as a verifier does:
# without its write lock, without making it the caller's. The reads; and the checks, which record in that changeset
# as its writer's do (run_checks its check rows, the others their jobs in its working copy) and edit nothing of it.
READ_OTHER = frozenset({
    "get_design_context", "list_cells", "get_checks", "get_history", "get_layout_info", "get_device_geometry",
    "query_shapes", "get_lvs_report", "check_device_integrity", "list_symbols", "render_schematic", "render_layout",
    "get_drc_report", "plot_waveform", "sample_waveform", "wait_for", "get_netlist", "get_drawing", "check_symbol",
    "get_intent", "get_rtl", "lint_rtl", "run_rtl_sim", "run_simulation", "cancel_job", "extract_parasitics",
    "get_parasitics", "check_specs", "sweep", "run_checks"})
READ_CHANGESET = {"type": "string", "description": "optional: another open changeset (its id, e.g. cs-1a2b3c) to read "
                  "and check instead of yours, as a verifier does: its working copy, without its write lock and without "
                  "making it yours (your own stays open in your session). What you start there (checks, extractions, "
                  "simulations) is recorded in that changeset as its writer's are; nothing of it is edited"}
for _t in TOOLS:
    if _t["name"] in READ_OTHER:
        _t["input_schema"] = {**_t["input_schema"], "properties": {**_t["input_schema"]["properties"],
                                                                   "changeset": READ_CHANGESET}}   # LIBCELL is shared


def specs(read_only: bool = False, pictures: bool = True) -> list[dict]:
    """The tools a client is offered. pictures=False leaves out those that answer with a PNG (render_*, plot_waveform),
    for a client that receives text only (the in-app assistant)."""
    return [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in TOOLS if not (read_only and t["write"]) and (pictures or not t.get("picture"))]


def _picture_type():
    from undine.agents.render import Picture
    return Picture


MORE = {   # how to ask for the rest of a long result (results.fit's _truncated.how)
    "get_netlist": "page with offset / limit, or ask for instances / nets; a testbench's code: code false or code_limit",
    "get_changeset": "ask for one cell (cell: lib/cell), without detail",
    "get_device_geometry": "ask for fewer layers, without polygons",
    "get_intent": "ask for ids or kinds",
    "get_design_context": "ask for fewer objects, or overview: false",
    "run_simulation": "ask for fewer signals; sample: fewer points, or sample_waveform on this job from a later start",
    "sample_waveform": "fewer points, or the rest from a later start (the time after the last row); all of them: "
                       "undine call --max-chars 0 into a file, or the Python client",
    "batch": "fewer ops, or reads of their own",
    "get_drawing": "ask with net, instances or region [x1, y1, x2, y2]",
    "check_specs": "ask with fewer specs (ids), corners, temperatures or conditions",
    "sweep": "ask with fewer specs (ids), vary values, corners, temperatures or conditions",
    "get_lvs_report": "get_netlist / get_layout_info for the details",
}


def _lease(c: ToolCtx, name: str, ahead: bool) -> None:
    """Takes or renews the write lease of the changeset a write tool writes (live.Leases), ahead of the tool and after
    it (a long one; open_changeset: the changeset it opened). open_changeset writes none ahead: it takes no lease of the
    changeset it leaves, and it may leave or continue one another writer holds (its answer says so: writer); but a
    writer that was stopped opens nothing."""
    if name == "open_changeset" and ahead:
        c.srv.leases.check_stopped(c.owner)
        return
    try:
        c.srv.leases.acquire(c.changeset, c.owner, c.owner_kind, c.actor.removeprefix("agent:"))
    except live.WriteLocked as e:
        if name != "open_changeset" or e.lease is None:
            raise


def _holds(c: ToolCtx, cid: str) -> bool:
    """Whether the caller writes `cid` now (holds its write lease). changeset= with the caller's own changeset reads it
    too unless it does: a verifier that once opened the changeset it checks (open_changeset id=) is not its writer, and
    must neither be refused while the writer writes nor take the lock (and commit the writer's work) when it lapsed. A
    caller with no owner (a script on the server) writes without a lease: its own changeset is its own."""
    if not c.owner:
        return True
    le = c.srv.leases.get(cid)
    return le is not None and le.owner == c.owner


def _reader(c: ToolCtx, name: str, cid) -> ToolCtx:
    """The context a call with changeset=cid runs in: that open changeset's working copy, read and checked (READ_OTHER),
    without its write lock and without making it the caller's (the caller's own changeset stays as it is)."""
    if name not in READ_OTHER:
        if not BY_NAME[name]["write"]:
            raise ToolError(f"{name} does not take changeset" + (": get_changeset(id=...) reads any changeset"
                                                                  if name == "get_changeset" else ""))
        raise ToolError(f"{name} does not take changeset: edits go into your own changeset (open_changeset); "
                        "changeset= reads and checks another's with the read and check tools")
    if not isinstance(cid, str) or not CID.fullmatch(cid):
        raise ToolError(f"changeset: an id such as cs-1a2b3c (list_changesets), not {cid!r}")
    try:
        meta = c.cs._load(c.project, cid)
    except (KeyError, ValueError, OSError) as e:
        raise ToolError(f"no changeset {cid} in {c.project}; list_changesets shows the open ones") from e
    if meta["status"] != "open":
        raise ToolError(f"changeset {cid} is {meta['status']}: its working copy is gone (get_changeset(id) has its "
                        "checks and evidence)")
    return replace(c, changeset=cid, reading=True, images=[], committed=[])


def call(c: ToolCtx, name: str, args: dict, limit: int | None = RESULT_CHARS) -> tuple[str, bool]:
    """Runs a tool; returns (result text, is_error). Errors the model can act on are results, not exceptions. A result
    longer than `limit` characters is shortened inside and stays valid JSON (results.py); None or 0: in full.
    `changeset` in the arguments (READ_OTHER): the call reads and checks that changeset (_reader) instead of yours."""
    t = BY_NAME.get(name)
    if t is None:
        return f"unknown tool {name}", True
    args = dict(args or {})
    r = c
    try:
        other = args.pop("changeset", None)
        if other not in (None, "") and (other != c.changeset or name in READ_OTHER and not _holds(c, other)):
            r = _reader(c, name, other)
        with live.acting(c.owner), live.reading(r.reading), live.operation(c.turn) as committed:
            r.committed = committed
            if t["write"] and not r.reading and c.owner and (c.changeset or name == "open_changeset"):
                _lease(c, name, ahead=True)                   # a stopped writer opens none
            r.images, r.limit = [], limit or None             # a tool that tells less of a large result (_fit) uses it
            res = t["fn"](r, _args(t, args))
            if isinstance(res, _picture_type()):           # a picture: the PNG goes beside the text (MCP clients)
                r.images = [res.png]
                res = {**res.info, "image_attached": True}
            if t["write"] and not r.reading and c.changeset and c.owner:   # open_changeset: the lease of the new one
                _lease(c, name, ahead=False)
            if r.reading and isinstance(res, dict):              # every answer says which changeset it read
                res = {"changeset": r.changeset, **res}
            elif r.reading and isinstance(res, str):
                res = f"changeset: {r.changeset}\n{res}"
        return results.text(res, limit, MORE.get(name, "")), False
    except live.WriteLocked as e:
        if e.lease is not None and c.owner_kind == "mcp":     # another writer's: it names who (live.holder)
            return (f"{e}. It is not your changeset to write now: open_changeset opens one of your own (list_changesets "
                    "says whose each is)"), True
        return str(e), True
    except ToolError as e:
        return str(e), True
    except Exception as e:  # a bug must not kill the session; the model sees it, the server log has the details
        import traceback
        traceback.print_exc()
        return f"internal error in {name}: {e}", True
    finally:
        if r is not c:                                 # what the reader's call drew and wrote is the call's
            c.images, c.committed = r.images, r.committed
