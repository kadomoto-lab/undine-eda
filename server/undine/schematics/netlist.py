"""The netlist of a cell: its connectivity as a view of its own, next to the schematic that draws it.

<cell>.netlist.yaml beside <cell>.sch:

    version: 1
    ports:                                    # in the schematic's order: the subcircuit's pin order
      - {name: A, type: ipin}
      - {name: Y, type: opin}
    instances:                                # in the schematic's order
      M1:
        symbol: sky130_fd_pr/pfet_01v8.sym
        props: {L: '0.15', W: '2', model: pfet_01v8, nf: '1'}
        pins: {D: Y, G: A, S: VDD, B: VDD}   # in the symbol's pin order
    code:                                     # simulation / netlist commands, in order
      - {name: s1, symbol: devices/code_shown.sym, props: {value: .tran 1n 1u}}
    pending: {M3.G: A}                        # connections the drawing does not show yet

Tools that do not need the drawing (the netlisters, LVS, schematic-driven layout, the agent's view of the circuit)
read this: `connectivity()`. The schematic editor writes both: every drawing edit is read back into the netlist
(`sync`), and a schematic changed outside Undine (Xschem) is read back the next time the netlist is read (the
server remembers which drawing each netlist was read from; the file holds connectivity only, so moving a symbol
changes the schematic and not this file).

Pending connections are what the netlist has and the drawing does not show (added through the netlist, e.g. by a
tool; a wire the router could not find): the schematic shows them as a ratsnest and never draws them by itself. A
connection drawn later replaces the pending one; a pin drawn to another net takes that net (the drawing wins).

Unlabeled nets keep their names across edits: a net of the drawing takes the name of the previous net it shares the
most pins with, so moving or redrawing a wire does not rename nets in the netlist. The file is written in a
deterministic order, so a change of the drawing alone (moving a symbol) does not change this file, and a change of the
circuit shows here as its own diff.
"""
from __future__ import annotations

import re
import threading
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import yaml

from undine.store import oplog
from undine.schematics import schematic, xschem
from undine.schematics.xschem import XComp

SUFFIX = ".netlist.yaml"
PLACED = "undine_placed"          # instance property of the drawing: "auto" while no person has moved it
DRAWING_ONLY = {"name", PLACED}   # drawing attributes, not part of the netlist
_AUTO = re.compile(r"^net\d+$")


def path_of(sch: Path) -> Path:
    return sch.with_name(sch.stem + SUFFIX)


def empty() -> dict:
    return {"version": 1, "ports": [], "instances": {}, "code": [], "pending": {}}


def normalize(raw: dict) -> dict:
    """The model of a file's YAML mapping: every key of empty() (a missing or null one empty), pending as
    {"inst.pin": net} strings (an empty pending of any kind, e.g. [], is none). The one reading of the file
    (load, and the merge in vcs/yamlmerge.py)."""
    m = empty()
    for k in m:
        if k in raw and raw[k] is not None:
            m[k] = raw[k]
    m["pending"] = {str(k): str(v) for k, v in (m["pending"] or {}).items()}
    return m


# libyaml reads the same data about ten times faster (every drawing edit reads the netlist back); its writer is not
# used, as its line breaks and quoting differ from PyYAML's in places and the file must not churn
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return normalize(yaml.load(path.read_text(errors="replace"), Loader=_LOADER) or {})


def dump(model: dict) -> str:
    out = {"version": 1, "ports": model["ports"], "instances": model["instances"]}
    if model["code"]:
        out["code"] = model["code"]
    if model["pending"]:
        out["pending"] = dict(sorted(model["pending"].items()))
    return yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=120, default_flow_style=None)


def _write(path: Path, model: dict) -> str:
    """The one place a netlist file is written: never into a protected tree (a PDK, a read-only library)."""
    if protected(path):
        raise PermissionError(f"{path.parent} is read-only (a PDK or a pinned dependency): its netlist is not written")
    text = dump(model)
    if not path.is_file() or path.read_text(errors="replace") != text:
        path.write_text(text)
    return text


# ------------------------------------------------------------------ from the drawing


def _labels(f) -> set[str]:
    """Net names the drawing states: wire labels and the lab of label / pin symbols."""
    out = {w[4].get("lab", "") for w in f.wires}
    out |= {c.props.get("lab", "") for c in f.comps}
    out.discard("")
    return out


def read_drawing(sch: Path, resolver: xschem.SymbolResolver, previous: dict | None) -> tuple[dict, list[str]]:
    """The netlist of a schematic as drawn, keeping the names of unlabeled nets and the pending connections of
    `previous` where the drawing still leaves them open. Returns (model, warnings)."""
    model, warnings, _, _ = _read(sch, resolver, previous)
    return model, warnings


def drawn_names(sch: Path, resolver: xschem.SymbolResolver) -> tuple[schematic.Connectivity, dict[str, str]]:
    """The drawing's connectivity and, for each of its nets, the netlist's name for it (an unlabeled net of the
    drawing is numbered in drawing order; the netlist keeps its name across edits)."""
    _, _, rename, conn = _read(sch, resolver, load(path_of(sch)))
    return conn, {n: rename.get(n, n) for n in set(conn.nets) | set(conn.wire_net)}


def _read(sch: Path, resolver: xschem.SymbolResolver, previous: dict | None):
    text = sch.read_text(errors="replace")
    f = xschem.parse(text)
    conn = schematic.extract(sch, resolver)
    prev = previous or empty()
    named = _labels(f)
    wired = set(conn.wire_net)

    # members of each drawn net, and which nets are unlabeled (their names are generated: net1, net2 ...)
    members: dict[str, set[tuple[str, str]]] = {}
    for d in conn.devices:
        for pn, net in d.nets.items():
            members.setdefault(net, set()).add((d.name, pn))
    floating = {next(iter(m)) for n, m in members.items()
                if len(m) == 1 and n not in named and n not in wired and _AUTO.match(n)}

    # an unlabeled net keeps the name of the previous net it shares the most drawn pins with
    prev_pending = prev["pending"]
    prev_members: dict[str, set[tuple[str, str]]] = {}
    for inst, rec in prev["instances"].items():
        for pn, net in (rec.get("pins") or {}).items():
            if f"{inst}.{pn}" not in prev_pending:
                prev_members.setdefault(net, set()).add((inst, pn))
    rename: dict[str, str] = {}
    taken = {n for n in members if not _AUTO.match(n) or n in named} | set(prev_pending.values())
    candidates = sorted(((len(m & prev_members.get(pn_, set())), n, pn_)
                         for n, m in members.items() if _AUTO.match(n) and n not in named
                         for pn_ in prev_members if (m & prev_members[pn_]) and (_AUTO.match(pn_) and pn_ not in named)),
                        key=lambda t: (-t[0], t[1], t[2]))
    for _, n, old in candidates:
        if n not in rename and old not in taken:
            rename[n] = old
            taken.add(old)
    k = 0
    for n in sorted((n for n in members if _AUTO.match(n) and n not in named and n not in rename),
                    key=lambda s: int(s[3:])):
        k += 1
        while f"net{k}" in taken:
            k += 1
        rename[n] = f"net{k}"
        taken.add(f"net{k}")

    model = empty()
    model["ports"] = [{"name": name, "type": typ} for name, typ in conn.ports]
    pending: dict[str, str] = {}
    for d in conn.devices:
        pins = {}
        for pn, _, _ in d.pins:
            drawn = d.nets.get(pn, "")
            key = f"{d.name}.{pn}"
            if key in prev_pending and (d.name, pn) in floating:
                pins[pn] = pending[key] = prev_pending[key]
            else:
                pins[pn] = rename.get(drawn, drawn)
        model["instances"][d.name] = {
            "symbol": d.comp.sym,
            "props": {k: str(v) for k, v in sorted(d.comp.props.items()) if k not in DRAWING_ONLY},
            "pins": pins,
        }
    for c, _sym in conn.code:
        model["code"].append({"name": c.props.get("name", ""), "symbol": c.sym,
                              "props": {k: str(v) for k, v in sorted(c.props.items()) if k not in DRAWING_ONLY}})
    model["pending"] = pending
    _remember_warnings(sch, text, conn.warnings)
    return model, conn.warnings, rename, conn


_WARNED: dict[tuple[str, str], list[str]] = {}   # (schematic, sha of the drawing) -> what reading it warned of


def _remember_warnings(sch: Path, text: str, warnings: list[str]) -> None:
    with _LOCK:
        if len(_WARNED) > 512:
            _WARNED.clear()
        _WARNED[(str(sch), oplog.sha(text))] = list(warnings)


def drawing_warnings(sch: Path, resolver: xschem.SymbolResolver) -> list[str]:
    """What reading the drawing into the netlist warned of: an instance whose symbol does not resolve (a symbol of
    another PDK) is left out of the netlist, so the netlist alone cannot tell it was ever drawn."""
    with _LOCK:
        if not sch.is_file():
            return []
        text = sch.read_text(errors="replace")
        key = (str(sch), oplog.sha(text))
        if key not in _WARNED:
            _remember_warnings(sch, text, schematic.extract(sch, resolver).warnings)
        return list(_WARNED[key])


_LOCK = threading.RLock()
_PROTECTED: set[Path] = set()      # read-only trees (PDKs, read-only libraries): never written, whatever the file mode


def protect(*dirs: Path) -> None:
    """Never write a netlist file under these directories. A PDK or a pinned dependency is often writable on disk (a
    PDK installed in the home directory), so the file mode alone does not say whether Undine may write there."""
    with _LOCK:
        for d in dirs:
            try:
                _PROTECTED.add(Path(d).resolve())
            except OSError:
                pass


def protected(sch: Path) -> bool:
    try:
        d = sch.parent.resolve()
    except OSError:
        return True
    return any(d == r or r in d.parents for r in _PROTECTED)


def writable(sch: Path) -> bool:
    if protected(sch):
        return False
    try:
        return sch.parent.is_dir() and (path_of(sch).exists() and path_of(sch).stat().st_mode & 0o200 != 0
                                        or not path_of(sch).exists() and (sch.parent.stat().st_mode & 0o200) != 0)
    except OSError:
        return False


_MEMO: dict[tuple, dict] = {}       # netlists read from a drawing and not written (a consumer, a read-only library)
_SYNCED: dict[str, tuple[str, str]] = {}   # netlist file -> (sha of the drawing it was read from, sha of the file)


def current(sch: Path, resolver: xschem.SymbolResolver, write: bool = True, on_import=None) -> dict:
    """The cell's netlist, read again from the drawing when the schematic changed since (an edit outside Undine, or
    a schematic without a netlist yet). Written back when `write` (editors, the project's own libraries); a consumer
    passes write=False and gets it from memory, so reading never creates files in a library (or a PDK)."""
    with _LOCK:
        npath = path_of(sch)
        model = load(npath)
        if not sch.is_file():
            if model is None:
                raise KeyError(f"no schematic {sch.stem}")
            return model
        sha = oplog.sha(sch.read_text(errors="replace"))
        file_sha = oplog.sha(dump(model)) if model is not None else ""
        if _SYNCED.get(str(npath)) == (sha, file_sha):
            return model
        key = (str(sch), sha, file_sha)
        if not write and key in _MEMO:
            return deepcopy(_MEMO[key])
        before = model
        model, _ = read_drawing(sch, resolver, model)
        if write and writable(sch):
            _SYNCED[str(npath)] = (sha, oplog.sha(_write(npath, model)))
            if on_import is not None and before is not None and dump(before) != dump(model):
                on_import(before, model)
        else:
            if len(_MEMO) > 512:
                _MEMO.clear()
            _MEMO[key] = deepcopy(model)
        return model


def sync(sch: Path, resolver: xschem.SymbolResolver) -> dict:
    """After an edit of the drawing: the netlist read from it again, written (kept in memory in a read-only tree)."""
    with _LOCK:
        npath = path_of(sch)
        before = load(npath)
        model, _ = read_drawing(sch, resolver, before)
        if not writable(sch):
            return current(sch, resolver, write=False)
        # a drawing edit that leaves the circuit as it is (a move) needs no writing, nor PyYAML's slow writer
        text = npath.read_text(errors="replace") if model == before else _write(npath, model)
        _SYNCED[str(npath)] = (oplog.sha(sch.read_text(errors="replace")), oplog.sha(text))
        return model


def changes(before: dict, after: dict) -> list[str]:
    """What changed between two netlists, in words (for the log of an edit made outside Undine)."""
    out = []
    a, b = before["instances"], after["instances"]
    out += [f"added {n}" for n in b if n not in a]
    out += [f"removed {n}" for n in a if n not in b]
    for n in a.keys() & b.keys():
        if a[n].get("pins") != b[n].get("pins"):
            moved = [f"{p}: {a[n]['pins'].get(p)} -> {net}" for p, net in b[n].get("pins", {}).items() if a[n]["pins"].get(p) != net]
            out.append(f"{n} connections {', '.join(moved)}")
        if a[n].get("props") != b[n].get("props") or a[n].get("symbol") != b[n].get("symbol"):
            out.append(f"{n} parameters")
    if before["ports"] != after["ports"]:
        out.append("ports " + ", ".join(p["name"] for p in after["ports"]))
    if before["code"] != after["code"]:
        out.append("netlist commands")
    return sorted(out)


class Watcher(threading.Thread):
    """Reads a schematic changed outside Undine (Xschem, a git checkout, an editor) back into its netlist as soon as it
    changes, not only the next time something reads it: polls the schematics of the projects' own libraries every
    `interval` seconds (polling also works where file systems send no change notifications, such as Windows drives
    under WSL). Undine's own edits are synced by the editor already, so they are not read twice. An import that changes
    the netlist is recorded in the library's operation log (op "import_drawing", actor "external"; not undoable)."""

    def __init__(self, srv, interval: float = 1.0):
        super().__init__(name="undine-netlist-watch", daemon=True)
        self.srv = srv
        self.interval = interval
        self.last: dict[str, tuple[int, int]] = {}
        self.stopped = threading.Event()

    def run(self) -> None:
        while not self.stopped.is_set():
            try:
                self.scan()
            except Exception as e:                      # a broken project must not stop the watch
                import sys
                print(f"undine: netlist watch: {e}", file=sys.stderr)
            self.stopped.wait(self.interval)

    def stop(self) -> None:
        self.stopped.set()

    def scan(self) -> list[str]:
        """One pass; returns the schematics read back (for tests)."""
        seen: dict[str, tuple[int, int]] = {}
        read = []
        for prj in self.srv.store.list():
            resolver = None
            for lib, spec in prj.library_specs().items():
                d = prj.lib_path(spec["path"]) if "path" in spec else None
                if d is None or not d.is_dir():
                    continue
                for sch in sorted(d.glob("*.sch")):
                    try:
                        st = sch.stat()
                    except OSError:
                        continue
                    sig = (st.st_mtime_ns, st.st_size)
                    seen[str(sch)] = sig
                    if self.last.get(str(sch)) == sig:
                        continue
                    resolver = resolver or xschem.SymbolResolver(prj.symbol_paths())
                    current(sch, resolver, write=True, on_import=lambda before, after, prj=prj, lib=lib, sch=sch:
                            self._log(prj.name, lib, sch.stem, before, after))
                    read.append(str(sch))
        self.last = seen
        return read

    def _log(self, project: str, lib: str, cell: str, before: dict, after: dict) -> None:
        log = self.srv.oplog(project, lib)
        log.append({"op": "import_drawing", "args": {}, "actor": "external", "cell": cell, "view": "netlist",
                    "reason": "the schematic was changed outside Undine: " + "; ".join(changes(before, after)[:12]),
                    "sha": oplog.sha(dump(after)), "effects": None})


# ------------------------------------------------------------------ for tools that read connectivity only


def connectivity(sch: Path, resolver: xschem.SymbolResolver, cell: str | None = None,
                 write: bool = True) -> schematic.Connectivity:
    """The netlist as a schematic.Connectivity (devices, nets, ports, code), pending connections included; no drawing
    (wire_net, wire_ids and net_points are empty)."""
    with _LOCK:
        model = current(sch, resolver, write)
        warnings = drawing_warnings(sch, resolver)
    base = sch.parent
    devices: list[schematic.Device] = []
    nets: dict[str, list[tuple[str, str]]] = {}
    for i, (name, rec) in enumerate(model["instances"].items()):
        comp = XComp(rec["symbol"], 0, 0, 0, 0, {"name": name.split("#")[0], **rec.get("props", {})})
        sym = schematic.resolve_symbol(resolver, comp, base)
        if sym is None:
            w = f"missing symbol {rec['symbol']} (instance {name})"
            if w not in warnings:
                warnings.append(w)
            continue
        typ = sym.global_props.get("type", "")
        pins = [(p.name, 0.0, 0.0) for p in schematic.sym_pins(sym)]
        d = schematic.Device(name, comp, i, sym, Path(rec["symbol"]).stem, typ, pins, dict(rec.get("pins") or {}))
        devices.append(d)
        for pn, net in d.nets.items():
            nets.setdefault(net, []).append((name, pn))
    code = []
    for c in model["code"]:
        comp = XComp(c["symbol"], 0, 0, 0, 0, {"name": c.get("name", ""), **c.get("props", {})})
        sym = schematic.resolve_symbol(resolver, comp, base)
        if sym is not None:
            code.append((comp, sym))
    ports = [(p["name"], p["type"]) for p in model["ports"]]
    for p, _ in ports:
        nets.setdefault(p, [])
    warnings += [w for w in supply_warnings(model, {d.name: supply_params(d.sym, d.comp.props) for d in devices}, sch)
                 if w not in warnings]
    return schematic.Connectivity(cell or sch.stem, devices, nets, ports, [], [], {}, code, warnings)


# ------------------------------------------------------------------ connections given as parameters

def supply_params(sym, props: dict) -> dict[str, str]:
    """The connections a symbol takes as parameters instead of pins: those of its Xschem `extra` attribute that its
    netlist format writes as nodes (the standard cells' VGND / VNB / VPB / VPWR; not `prefix`). {parameter: net}."""
    if sym is None or not sym.global_props.get("extra"):
        return {}
    fmt = sym.global_props.get("format", "") + " " + sym.global_props.get("lvs_format", "")
    values = schematic.inst_props(XComp("", 0, 0, 0, 0, props), sym)
    out = {}
    for name in sym.global_props["extra"].split():
        net = str(values.get(name, "")).strip()
        if net and re.search(rf"@{re.escape(name)}(?![A-Za-z0-9_\\])", fmt) and re.fullmatch(r"[^\s@{}\"']+", net):
            out[name] = net
    return out


def supplies(sch: Path, resolver: xschem.SymbolResolver, model: dict) -> dict[str, dict[str, str]]:
    """supply_params of every instance of a netlist model that has some: {instance: {parameter: net}}."""
    out = {}
    for name, rec in model["instances"].items():
        comp = XComp(rec["symbol"], 0, 0, 0, 0, dict(rec.get("props") or {}))
        if (got := supply_params(schematic.resolve_symbol(resolver, comp, sch.parent), comp.props)):
            out[name] = got
    return out


def code_nodes(model: dict) -> set[str]:
    """The nodes the code blocks of a netlist model name, in lower case (SPICE does not tell case apart): those of
    their element lines (Vdd vdd 0 1.8) and .global lines. Generous: a value or a model name may be among them."""
    out: set[str] = set()
    for c in model["code"]:
        text = re.sub(r"\n[ \t]*\+", " ", str((c.get("props") or {}).get("value", "")))
        for line in text.splitlines():
            tokens = line.split(";")[0].split()
            if not tokens or not (tokens[0][0].isalpha() or tokens[0].lower() == ".global"):
                continue
            out |= {t.lower() for t in tokens[1:] if "=" not in t}
    return out


def supply_warnings(model: dict, params: dict[str, dict[str, str]], sch: Path | None = None) -> list[str]:
    """A supply parameter naming a net nothing else in the schematic is on (no pin, no port, no code block's node, no
    label of the drawing `sch`): netlisted, it becomes a node of its own, the supply pin floats (VPWR=VPWR left at the
    template's default, a misspelt vdd). Names compare without case, as SPICE does."""
    nets = {n for rec in model["instances"].values() for n in (rec.get("pins") or {}).values()}
    nets = {n.lower() for n in nets | {p["name"] for p in model["ports"]}} | {"0", "gnd"}
    bad: dict[tuple[str, str], list[str]] = {}
    for inst, got in params.items():
        for param, net in got.items():
            if net.lower() not in nets:
                bad.setdefault((param, net), []).append(inst)
    if bad:                                          # rare: only then the code blocks and the drawing's labels are read
        nets |= code_nodes(model)
        if sch is not None and sch.is_file():
            nets |= {n.lower() for n in _labels(xschem.parse(sch.read_text(errors="replace")))}
        bad = {k: v for k, v in bad.items() if k[1].lower() not in nets}
    out = []
    for (param, net), insts in bad.items():         # one line per parameter and net: a cell of hundreds of gates stays legible
        who = ", ".join(insts[:8]) + (f" and {len(insts) - 8} more" if len(insts) > 8 else "")
        out.append(f"{param}={net} on {who} names no net of the schematic (no pin, port, label or code block is on "
                   f"{net}), so that supply pin connects to nothing; set it to the supply net (e.g. vdd / vss)")
    return out


def pending_links(sch: Path, resolver: xschem.SymbolResolver, model: dict) -> list[list]:
    """For the schematic view: each pending connection as a line from its pin to the nearest point already on its
    net (another pin, a wire, a label), or to the nearest other pending pin of the net: [inst, pin, net, x1, y1, x2, y2]."""
    if not model["pending"]:
        return []
    conn = schematic.extract(sch, resolver)
    at: dict[tuple[str, str], tuple[float, float]] = {(d.name, pn): (x, y) for d in conn.devices for pn, x, y in d.pins}
    on_net: dict[str, list[tuple[float, float]]] = {n: list(pts) for n, pts in conn.net_points.items()}
    pend_pts: dict[str, list[tuple[str, tuple[float, float]]]] = {}
    for key, net in model["pending"].items():
        inst, _, pin = key.rpartition(".")
        if (inst, pin) in at:
            pend_pts.setdefault(net, []).append((key, at[(inst, pin)]))
    out = []
    for net, items in pend_pts.items():
        fixed = on_net.get(net, [])
        for key, (x, y) in items:
            targets = fixed or [p for k2, p in items if k2 != key]
            if not targets:
                continue
            tx, ty = min(targets, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)
            inst, _, pin = key.rpartition(".")
            out.append([inst, pin, net, x, y, tx, ty])
    return out


# ------------------------------------------------------------------ edits of the netlist itself


class NetlistEditor:
    """Edits the connections the drawing does not show (pending), recorded in the library's operation log (view
    "netlist"); everything drawn is edited in the schematic, which writes the netlist through `sync`."""

    view_kind = "netlist"

    def __init__(self, sch: Path, resolver: xschem.SymbolResolver, log: oplog.OpLog | None = None):
        self.sch = sch
        self.path = path_of(sch)
        self.resolver = resolver
        self.log = log
        self.cell = sch.stem
        self.lock = threading.RLock()
        self._meta: dict = {"op": "edit", "args": {}, "actor": "user"}

    @contextmanager
    def recording(self, op: str, args: dict, actor: str = "user", reason: str | None = None,
                  changeset: str | None = None):
        with self.lock:
            self._meta = {"op": op, "args": args, "actor": actor, "reason": reason, "changeset": changeset}
            try:
                yield
            finally:
                self._meta = {"op": "edit", "args": {}, "actor": "user"}

    def model(self) -> dict:
        return current(self.sch, self.resolver)

    def _apply_pending(self, pending: dict[str, str]) -> dict:
        """Writes the netlist with this set of pending connections (the drawing gives everything else)."""
        with _LOCK:
            prev = self.model()
            prev = {**prev, "pending": pending}
            model, _ = read_drawing(self.sch, self.resolver, prev)
            text = _write(self.path, model)
            _SYNCED[str(self.path)] = (oplog.sha(self.sch.read_text(errors="replace")), oplog.sha(text))
            return model

    def _commit(self, before: dict, after: dict) -> list[dict]:
        if self.log is not None:
            self.log.guard()
        effects = []
        for key in sorted(set(before) | set(after)):
            a, b = before.get(key), after.get(key)
            if a == b:
                continue
            if a is None:
                effects.append({"k": "nl", "id": f"pending.{key}", "do": "add", "obj": b})
            elif b is None:
                effects.append({"k": "nl", "id": f"pending.{key}", "do": "del", "obj": a})
            else:
                effects.append({"k": "nl", "id": f"pending.{key}", "do": "mod", "changes": {"value": [a, b]}})
        if not effects:
            return []
        model = self._apply_pending(after)
        if self.log is not None:
            self.log.append({**self._meta, "cell": self.cell, "view": self.view_kind,
                             "sha": oplog.sha(dump(model)), "effects": effects})
        return effects

    def _pins(self, model: dict, inst: str, pin: str) -> None:
        rec = model["instances"].get(inst)
        if rec is None:
            raise KeyError(f"no instance {inst} (there are: {', '.join(model['instances']) or 'none'})")
        if pin not in rec["pins"]:
            raise KeyError(f"{inst} has no pin {pin} (its pins: {', '.join(rec['pins'])})")

    def connect(self, inst: str, pin: str, net: str) -> str:
        """Puts a pin on a net without drawing it (a pending connection, shown as a ratsnest). Returns "pending",
        or "drawn" when the drawing already connects it there. A pin drawn to another net is refused: the drawing
        shows it, so change the drawing."""
        net = (net or "").strip()
        if not net:
            raise ValueError("a net name is required")
        with self.lock:
            m = self.model()
            self._pins(m, inst, pin)
            key = f"{inst}.{pin}"
            drawn = m["instances"][inst]["pins"][pin]
            if key not in m["pending"] and not self._floating(m, inst, pin):
                if drawn == net:
                    return "drawn"
                raise ValueError(f"{inst}:{pin} is drawn to {drawn}: change the wire in the schematic to move it")
            self._commit(m["pending"], {**m["pending"], key: net})
            return "pending"

    def connect_many(self, pins: list[tuple[str, str, str]]) -> dict[str, str]:
        """connect for several pins in one operation: [(inst, pin, net)]; {inst.pin: "pending" | "drawn"}."""
        with self.lock:
            m = self.model()
            pending = dict(m["pending"])
            out = {}
            for inst, pin, net in pins:
                self._pins(m, inst, pin)
                key = f"{inst}.{pin}"
                drawn = m["instances"][inst]["pins"][pin]
                if key not in m["pending"] and not self._floating(m, inst, pin):
                    if drawn != net:
                        raise ValueError(f"{inst}:{pin} is drawn to {drawn}: change the wire in the schematic to move it")
                    out[key] = "drawn"
                    continue
                pending[key] = net
                out[key] = "pending"
            self._commit(m["pending"], pending)
            return out

    def disconnect(self, inst: str, pin: str) -> None:
        with self.lock:
            m = self.model()
            self._pins(m, inst, pin)
            key = f"{inst}.{pin}"
            if key not in m["pending"]:
                raise ValueError(f"{inst}:{pin} has no pending connection (a drawn one is removed in the schematic)")
            self._commit(m["pending"], {k: v for k, v in m["pending"].items() if k != key})

    def _floating(self, m: dict, inst: str, pin: str) -> bool:
        net = m["instances"][inst]["pins"][pin]
        if not _AUTO.match(net):
            return False
        users = [(i, p) for i, r in m["instances"].items() for p, n in r["pins"].items() if n == net]
        return users == [(inst, pin)] and not any(p["name"] == net for p in m["ports"])

    # ------------------------------------------------------------ history

    def _apply(self, pending: dict, effects: list[dict], backwards: bool) -> dict:
        out = dict(pending)
        for e in (reversed(effects) if backwards else effects):
            if e.get("k") != "nl":
                continue
            key = e["id"].split(".", 1)[1]
            do = {"add": "del", "del": "add", "mod": "mod"}[e["do"]] if backwards else e["do"]
            if do == "add":
                out[key] = e["obj"]
            elif do == "del":
                if key not in out:
                    raise ValueError(f"conflict: the pending connection {key} is gone (drawn since?)")
                out.pop(key)
            else:
                old, new = e["changes"]["value"]
                out[key] = old if backwards else new
        return out

    def replay(self, effects: list[dict], meta: dict) -> None:
        with self.lock:
            m = self.model()
            self._meta = meta
            try:
                self._commit(m["pending"], self._apply(m["pending"], effects, False))
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
        m = self.model()
        model = self._apply_pending(self._apply(m["pending"], rec["effects"], backwards))
        self.log.append({"op": "undo" if backwards else "redo", "of": rec["seq"], "actor": self._meta.get("actor", "user"),
                         "cell": self.cell, "view": self.view_kind, "sha": oplog.sha(dump(model)), "effects": None})
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


def model_copy(model: dict) -> dict:
    return deepcopy(model)
