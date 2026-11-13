"""Symbol edits and their dependent schematic wiring form one reversible operation.

Plan using an in-memory symbol overlay, then commit files and operation records
together. A dependent view may not be undone separately from its symbol.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
import uuid

from undine.server import live
from undine.store import oplog
from undine.schematics import netlist, schematic, symedit, xschem


def canonical(text: str) -> str:
    """The file as Undine writes it (xschem.canonical order), so a fingerprint does not depend on element order."""
    return xschem.dump(xschem.canonical(xschem.parse(text)))


def fingerprint(text: str | None) -> str | None:
    return oplog.sha(canonical(text)) if text is not None else None


class OverlayResolver(xschem.SymbolResolver):
    def __init__(self, paths, path: Path, model: xschem.XFile):
        super().__init__(paths)
        self.path, self.model = path.resolve(), model

    def load(self, name, base):
        found = self.find(name, base)
        if found is not None and found.resolve() == self.path:
            return self.model
        return super().load(name, base)


def _key(member: dict) -> tuple[str, str, str]:
    return member["lib"], member["cell"], member["view"]


def _member(key: tuple[str, str, str]) -> dict:
    return dict(zip(("lib", "cell", "view"), key))


def _interface(model):
    return model.global_props.get("type", ""), [(p.name, p.dir) for p in schematic.sym_pins(model)]


def _positions(model):
    return [(p.x, p.y) for p in schematic.sym_pins(model)]


@contextmanager
def _locked(srv, project, caller, extra):
    """Include all existing schematics so another edit cannot attach a new user mid-plan."""
    prj = srv.project(project)
    editors = {caller: srv.editor(project, *caller)}
    for member in extra:
        key = _key(member)
        editors[key] = srv.editor(project, *key)
    for lib in prj.libraries().values():
        for path in sorted(lib.path.glob("*.sch")):
            key = (lib.name, path.stem, "schematic")
            editors.setdefault(key, srv.editor(project, *key))
    with ExitStack() as stack:
        for ed in sorted(set(editors.values()), key=lambda ed: str(ed.path.resolve())):
            stack.enter_context(ed.lock)
        for log in sorted({ed.log for ed in editors.values()}, key=lambda log: str(log.path)):
            stack.enter_context(log.lock)
        yield prj, editors


def _consumers(prj, editors, source):
    path = source.path.resolve()
    resolver = xschem.SymbolResolver(prj.symbol_paths())
    found, seen = {}, set()
    for key, ed in editors.items():
        if key[2] != "schematic" or not ed.path.is_file() or ed.path.resolve() in seen:
            continue
        f = ed._load()
        if any(c.embedded is None and (p := resolver.find(c.sym, ed.path.parent)) is not None
               and p.resolve() == path for c in f.comps):
            found[key] = f
            seen.add(ed.path.resolve())
    return found


def placing(prj, symbols) -> list[Path]:
    """The schematics of the project's libraries that place one of the symbol files `symbols`: those whose netlists
    a change of the symbols' pins changes."""
    want = {Path(p).resolve() for p in symbols}
    resolver = xschem.SymbolResolver(prj.symbol_paths())
    out = []
    for lib in prj.libraries().values():
        for sch in sorted(lib.path.glob("*.sch")):
            f = xschem.parse(sch.read_text(errors="replace"))
            if any(c.embedded is None and (p := resolver.find(c.sym, sch.parent)) is not None and p.resolve() in want
                   for c in f.comps):
                out.append(sch)
    return out


def _by_name(model) -> dict[str, tuple[float, float]]:
    return {p.name: (p.x, p.y) for p in schematic.sym_pins(model)}


def _users(ed, model, source) -> list[int]:
    """The instances of the symbol `source` in a consumer's model."""
    path = source.path.resolve()
    return [ci for ci, c in enumerate(model.comps) if c.embedded is None
            and (p := ed.resolver.find(c.sym, ed.path.parent)) is not None and p.resolve() == path]


def _attached(ed, model, resolver, ci: int, pin: str) -> bool:
    """Whether something is drawn to pin `pin` of instance `ci` in a consumer: a wire through its point, or another
    terminal (a pin, a label, a port) on it."""
    terms = ed._pins(model, resolver)
    at = next((point for (i, _), name, point, _ in terms if i == ci and name == pin), None)
    if at is None:
        return False
    return (any(schematic._on_segment(*at, *w[:4]) for w in model.wires)
            or any(point == at and not (i == ci and name == pin) for (i, _), name, point, _ in terms))


def _pending(ed) -> dict[str, str]:
    """A consumer's pending connections ({"inst.pin": net}: in its netlist, not drawn), as its netlist has them now."""
    try:
        return netlist.current(ed.path, ed.resolver, write=False)["pending"]
    except (KeyError, OSError, ValueError):
        return {}


def _pin_names(model) -> list[str]:
    return [p.name for p in schematic.sym_pins(model)]


def _plan_consumers(prj, editors, source, old, new, consumers):
    """The consumers' wiring for a changed symbol. Their connections stay as they are: a moved pin's wiring follows
    it (SchEditor.plan_symbol_change), pin order and directions do not matter to them (a netlist connects by pin name),
    a new pin must land on nothing drawn, and a pin taken out (or renamed) must have nothing drawn to it nor a pending
    connection (one the netlist has and the drawing does not show)."""
    if consumers and old.global_props.get("type", "") != new.global_props.get("type", ""):
        names = ", ".join(f"{lib}/{cell}" for lib, cell, _ in consumers)
        raise ValueError(f"This symbol is used by {names}: its type cannot change.")
    before, after = _by_name(old), _by_name(new)
    if before == after:
        return {}
    old_resolver = OverlayResolver(prj.symbol_paths(), source.path, old)
    new_resolver = OverlayResolver(prj.symbol_paths(), source.path, new)
    mid = deepcopy(old)                       # the old pins at their new places: what the wiring has to follow
    mid.boxes = [(b[0], *_shift(b, after[b[5]["name"]]), b[5])
                 if b[0] == schematic.PIN_LAYER and b[5].get("name", "") in after else b for b in old.boxes]
    mid_resolver = OverlayResolver(prj.symbol_paths(), source.path, mid)
    gone, added = [n for n in before if n not in after], [n for n in after if n not in before]
    result = {}
    for key, model in consumers.items():
        ed = editors[key]
        users = _users(ed, model, source)
        pending = _pending(ed) if users and gone else {}
        ids = xschem.comp_ids(model)
        for ci in users:
            for name in gone:
                if _attached(ed, model, old_resolver, ci, name):
                    raise ValueError(f"{key[0]}/{key[1]}: pin {name} of {model.comps[ci].props.get('name', '?')} is "
                                     f"connected; the symbol cannot drop or rename it until that connection is removed")
                if (net := pending.get(f"{ids[ci]}.{name}")) is not None:
                    raise ValueError(f"{key[0]}/{key[1]}: pin {name} of {ids[ci]} is connected to {net} (a pending "
                                     "connection, not drawn); the symbol cannot drop or rename it until that connection "
                                     "is removed (disconnect_pins)")
        try:
            planned = ed.plan_symbol_change(model, old_resolver, mid_resolver) if _by_name(mid) != before else deepcopy(model)
        except ValueError as exc:
            raise ValueError(f"Cannot preserve connections in {key[0]}/{key[1]}: {exc}") from exc
        for ci in users:
            for name in added:
                if _attached(ed, planned, new_resolver, ci, name):
                    raise ValueError(f"{key[0]}/{key[1]}: the new pin {name} of {planned.comps[ci].props.get('name', '?')} "
                                     "would land on a wire or another pin there; move that instance or its wiring first")
        if oplog.diff(model, planned):
            if prj.library(key[0]).readonly:
                raise PermissionError(f"Cannot move the pins: dependent schematic {key[0]}/{key[1]} is read-only")
            result[key] = planned
    return result


def _shift(b: tuple, to: tuple[float, float]) -> tuple[float, float, float, float]:
    """A pin box (layer, x1, y1, x2, y2, props) moved so its centre is `to`."""
    dx, dy = to[0] - (b[1] + b[3]) / 2, to[1] - (b[2] + b[4]) / 2
    return b[1] + dx, b[2] + dy, b[3] + dx, b[4] + dy


@contextmanager
def _rollback_files(paths):
    originals = {path: path.read_bytes() if path.is_file() else None for path in paths}
    try:
        yield
    except Exception:
        for path, data in originals.items():
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
        raise


def _commit(editors, plans, records):
    """Files/logs roll back together on a refused lease, filesystem failure or log failure."""
    changed = [editors[key] for key in plans]
    for ed in changed:
        ed.log.guard()
    paths = {ed.path for ed in changed} | {ed.log.path for ed in changed}
    with _rollback_files(paths), live.operation():
        for key, text in plans.items():
            ed = editors[key]
            ed.log.guard()
            ed.path.write_text(text)
            ed.log.append(records[key])
            ed._net_cache = None
            ed._sync_netlist()                 # a moved pin can change what the schematic connects


def _reread(editors, consumers, old, new) -> None:
    """A pin added, taken out, renamed or reordered changes the netlists of the schematics that place the symbol though
    their drawing stays as it was: they are read again, every one of them (a netlist without a pin the symbol has would
    netlist it on node 0). After the editors forgot the symbols they had read."""
    if _pin_names(old) == _pin_names(new):
        return
    for key in consumers:
        editors[key]._net_cache = None
        editors[key]._sync_netlist()


def _response(editors, caller, result, affected, reread=None):
    # Clear cached symbol definitions in every held editor after either direction of an edit.
    for ed in editors.values():
        ed.resolver.cache.clear()
    if reread is not None:
        _reread(editors, *reread)
    return {"result": result, "view": editors[caller].view(caller[2]),
            "affected": [_member(key) for key in sorted(set(affected))]}


def _top(ed, backwards):
    done, redo = ed.log.stacks(ed.cell, ed.view_kind)
    return (done[-1] if backwards else redo[-1]) if (done if backwards else redo) else None


def _group_step(prj, editors, caller, record, backwards, actor, reason, changeset):
    group = record["symbol_group"]
    members = [_key(m) for m in group["members"]]
    plans, records = {}, {}
    for key in members:
        ed = editors[key]
        if prj.library(key[0]).readonly:
            raise PermissionError(f"Dependent view {key[0]}/{key[1]} is read-only")
        top = _top(ed, backwards)
        if not top or (top.get("symbol_group") or {}).get("id") != group["id"]:
            raise ValueError(f"Cannot {'undo' if backwards else 'redo'} the symbol and its wiring together: "
                             f"undo later edits in {key[0]}/{key[1]} ({key[2]}) first.")
        expected = top["symbol_after" if backwards else "symbol_before"]
        text = ed.path.read_text() if ed.path.is_file() else None
        if fingerprint(text) != expected:
            raise ValueError(f"Dependent view {key[0]}/{key[1]} ({key[2]}) changed outside this operation; "
                             "the symbol and wiring were not changed.")
        f = xschem.parse(text or "")
        oplog.apply(f, top["effects"], backwards)
        plans[key] = xschem.dump(xschem.canonical(f) if key[2] == "schematic" else f)
        records[key] = {"op": "undo" if backwards else "redo", "of": top["seq"], "actor": actor,
                        "reason": reason, "changeset": changeset, "cell": key[1], "view": key[2],
                        "sha": oplog.sha(plans[key]), "effects": None, "symbol_group": group}
    source_key = _key(group["source"])
    source = editors[source_key]
    consumers = _consumers(prj, editors, source)
    # A newly placed consumer is not covered by the original history bundle. Never
    # move its terminals out from under its wiring during an old operation's Undo.
    additions = {key: f for key, f in consumers.items() if key not in plans}
    following = _plan_consumers(prj, editors, source, source._load(), xschem.parse(plans[source_key]), additions)
    if following:
        names = ", ".join(f"{key[0]}/{key[1]}" for key in following)
        raise ValueError(f"New dependent wiring in {names} is not part of this operation; "
                         "undo its later edits before changing the symbol history.")
    before = source._load()
    _commit(editors, plans, records)
    return _response(editors, caller, True, [*members, *consumers],
                     (consumers, before, xschem.parse(plans[source_key])))


def schematic_ports(srv, project: str, lib: str, cell: str) -> list[tuple[str, str]]:
    """The ports of a cell's schematic in order (name, ipin | opin | iopin): the pins its symbol should have."""
    prj = srv.project(project)
    sch = prj.library(lib).path / f"{cell}.sch"
    if not sch.is_file():
        raise KeyError(f"{lib}/{cell} has no schematic: a symbol takes its pins from the schematic's ports")
    return schematic.connectivity(sch, xschem.SymbolResolver(prj.symbol_paths()), cell).ports


def from_schematic(srv, project: str, lib: str, cell: str, sides: dict[str, str] | None = None,
                   regenerate: bool = False, actor: str = "user", reason: str | None = None,
                   changeset: str | None = None, op: str = "sync_symbol", order_only: bool = False) -> dict:
    """Creates or updates a cell's symbol from its schematic's ports (symedit.from_ports): pin order, directions,
    missing and extra pins, sides. One operation with the wiring of the schematics that place the symbol, as a symbol
    edit is (a moved pin's wires follow it); their netlists are read again, as the pins they connect may change.
    `order_only`: the pins' order alone (symedit.reorder_pins), which no schematic placing the symbol has to follow."""
    caller = (lib, cell, "symbol")
    with srv.changeset_lock:
        prj = srv.project(project)
        if prj.library(lib).readonly or project.endswith("~base"):
            raise PermissionError("library is read-only")
        ports = schematic_ports(srv, project, lib, cell)
        args = {"order": [n for n, _ in ports]} if order_only else {"sides": sides or {}, "regenerate": bool(regenerate)}
        initial = srv.editor(project, *caller)
        if not initial.path.is_file():
            new = xschem.make_symbol(ports, sides)
            with initial.recording(op, args, actor, reason, changeset):
                initial.create_file(xschem.dump(new))
            return {"created": True, "order": [p.name for p in schematic.sym_pins(new)],
                    "sides": symedit.pin_sides(new), "affected": []}
        with _locked(srv, project, caller, []) as (prj, editors):
            source = editors[caller]
            source.log.guard()
            before = source._load()
            after, report = (symedit.reorder_pins(before, [n for n, _ in ports]) if order_only
                             else symedit.from_ports(before, ports, sides, regenerate))
            after = xschem.parse(xschem.dump(after))
            consumers = _consumers(prj, editors, source)
            following = _plan_consumers(prj, editors, source, before, after, consumers)
            plans, effects = {}, {}
            for key, model in {caller: after, **following}.items():
                text = xschem.dump(xschem.canonical(model) if key[2] == "schematic" else model)
                fx = oplog.diff(editors[key]._load(), xschem.parse(text))
                if fx:
                    plans[key], effects[key] = text, fx
            if plans:
                group = {"id": "sym-" + uuid.uuid4().hex, "source": _member(caller), "members": [_member(k) for k in plans]}
                records = {key: {"op": op if key == caller else "symbol_follow",
                                 "args": args if key == caller else {"symbol": f"{lib}/{cell}"},
                                 "actor": actor, "reason": reason, "changeset": changeset, "cell": key[1], "view": key[2],
                                 "sha": oplog.sha(text), "effects": effects[key], "symbol_group": group,
                                 "symbol_before": fingerprint(editors[key]._text()), "symbol_after": fingerprint(text)}
                           for key, text in plans.items()}
                _commit(editors, plans, records)
            for ed in editors.values():
                ed.resolver.cache.clear()
            _reread(editors, consumers, before, after)
            report["changed"] = bool(plans)
            report["affected"] = [f"{k[0]}/{k[1]}" for k in sorted(following)]
            report["consumers"] = [f"{k[0]}/{k[1]}" for k in sorted(consumers)]
            return report


def edit(srv, project: str, lib: str, cell: str, view: str, op: str, values: dict,
         actor: str = "user", reason: str | None = None, changeset: str | None = None):
    """Symbol primitives, and grouped Undo/Redo entered from any participating editor."""
    caller = (lib, cell, view)
    with srv.changeset_lock:
        prj = srv.project(project)
        if prj.library(lib).readonly or project.endswith("~base"):
            raise PermissionError("library is read-only")
        initial = srv.editor(project, *caller)
        backwards = op == "undo"
        with initial.lock:
            top = _top(initial, backwards) if op in ("undo", "redo") else None
        group = (top or {}).get("symbol_group")
        with _locked(srv, project, caller, group["members"] if group else []) as (prj, editors):
            source = editors[caller]
            source.log.guard()
            if op in ("undo", "redo"):
                top = _top(source, backwards)
                if top and top.get("symbol_group"):
                    return _group_step(prj, editors, caller, top, backwards, actor, reason, changeset)
                consumers = _consumers(prj, editors, source) if view == "symbol" else {}
                if top and consumers:
                    if any(e["k"] == "v" for e in top["effects"]):
                        raise ValueError("This symbol is used by other circuits; remove those instances before undoing its creation.")
                    candidate = source._load()
                    oplog.apply(candidate, top["effects"], backwards)
                    old = source._load()
                    if _interface(old) != _interface(candidate) or _positions(old) != _positions(candidate):
                        raise ValueError("This symbol history predates connected editing. Edit its pin positions directly "
                                         "so the dependent circuits can be updated together.")
                with source.recording(op, {}, actor, reason, changeset):
                    result = source.undo() if backwards else source.redo()
                return _response(editors, caller, result, [caller, *consumers])
            if view != "symbol" or not op.startswith("symbol_"):
                raise ValueError("use symbol drawing operations for a symbol view")
            if not source.path.is_file():
                raise KeyError(f"no symbol for {lib}/{cell}")
            before = source._load()
            after = deepcopy(before)
            result = symedit.edit(after, op, values)
            after = xschem.parse(xschem.dump(after))
            consumers = _consumers(prj, editors, source)
            following = _plan_consumers(prj, editors, source, before, after, consumers)
            models = {caller: after, **following}
            plans, effects = {}, {}
            for key, model in models.items():
                text = xschem.dump(xschem.canonical(model) if key[2] == "schematic" else model)
                fx = oplog.diff(editors[key]._load(), xschem.parse(text))
                if fx:
                    plans[key], effects[key] = text, fx
            if plans:
                group = {"id": "sym-" + uuid.uuid4().hex, "source": _member(caller),
                         "members": [_member(key) for key in plans]}
                records = {}
                for key, text in plans.items():
                    records[key] = {"op": op if key == caller else "symbol_follow", "args": values if key == caller else {"symbol": f"{lib}/{cell}"},
                                    "actor": actor, "reason": reason, "changeset": changeset,
                                    "cell": key[1], "view": key[2], "sha": oplog.sha(text), "effects": effects[key],
                                    "symbol_group": group, "symbol_before": fingerprint(editors[key]._text()),
                                    "symbol_after": fingerprint(text)}
                _commit(editors, plans, records)
            return _response(editors, caller, result, [caller, *consumers], (consumers, before, after))
