"""Three-way merge and semantic diff of a cell's YAML views: the netlist (`<cell>.netlist.yaml`, kind "netlist") and
the design intent (`<cell>.intent.yaml`, kind "intent"); docs/design/merge.md.

Each side is read the way its module reads the file (netlist.normalize, intent.normalize) and the merged model is
written the way the module writes it (netlist.dump, intent.dump; an empty intent is an empty file), so the result reads
like a file Undine wrote. A merged model equal to one side's keeps that side's bytes.

Netlist view (netlist.py). Ports, instances and code are derived from the schematic and are read again from the merged
drawing the next time Undine reads the netlist; only `pending` (connections the drawing does not show) exists nowhere
else. So only pending conflicts are conflicts:

    pending     per "inst.pin", the net one value; also a pin one side  conflict "pending:M3.G"
                connects (adds or moves its pending net) while the
                other draws it to another net: the merged drawing
                shows the drawn net, and reading it drops the pending
                one
    ports       per name, the entry one value                           not reported: ours is kept, and one
    instances   per name: symbol one value, props and pins per key          warning says these parts follow the
    code        per name, like an instance                                  merged schematic

A pin pending in the result is written on its pending net, as netlist.read_drawing writes it; a pin whose pending
conflict is decided for the side without a pending connection gets that side's net. Neither is in the warning; a pin
the sides drew differently is, even where a side had it pending before. A side drew a pin when it has no pending
connection on it and the pin is not on a lone net (unnamed, netlist._AUTO, no other pin on it, not a port: a pin left
floating, as NetlistEditor tells it). A pin on base's net counts as drawn only where base did not show it drawn (had it
pending or floating): read_drawing names a new unnamed net after the old one it shares the most pins with, so a
floating pin wired to another keeps its own netN. The file tells a drawn pin from a floating one by its net only: a pin
drawn to a wire that ends nowhere is not seen as drawn.

Intent (intent.py). Item ids are unique in the whole file, so an item is identified by its id wherever it sits; the
view it sits in (None for the cell intent) is one more value of it ("item:<id>:view"):

    notes       text, git's line merge (git merge-file)                 "notes" (overlapping lines, or no git)
    ports       per name, then per field                                "port:inp", "port:inp:side"
    supplies    per net, the role one value                             "supply:vdd"
    items       per id (delete / modify), then per field: text, kind,   "item:c1", "item:c1:text"
                strength, reason, rule, spec, source one value each;
                serves a set: both sides' additions and removals
    view items  the same, in views.<view>.items                         "view:layout:item:c3",
                                                                        "view:layout:item:c3:text"

Ids are allocated in sequence (intent.next_id: one more than the highest of the prefix, a deleted id is not handed out
again), so both sides adding `c3` is common: when the two are different items, theirs' item takes the id after the
highest of its prefix on any side (c4, unique in the whole file), and the serves of the items theirs added or changed
follow it. Two items are one (_same_item) when they have the same kind and the same text, the same whole rule, the same
spec measure on the same testbench and output, or the same source with its request (not only who added it: every item
a person adds has source {by: user}). An id need not be a string (intent.normalize takes id: 7): it is numbered by its
text, like intent.next_id does, and the new id is a string ('8'). Since an id is not reused, an item deleted on one
side and edited on the other is a delete / modify conflict, not an edit of a new item. The case left: a side that
deletes the highest-numbered item (c3) and adds one gets c3 again, which cannot be told from an edit of c3: the other
side's edits of the old c3 merge into the new one field by field.

A renumbering stays on the branch that merged: a merged b, so a has its c3 and b's as c4, while b still calls its item
c3. Two later merges follow it rather than match by id:

    merging again   base (b's old tip) has c3 = b's item: on the side   _follow; "item c4: theirs' item c3 is
    (either way)    whose c3 is no longer base's c3 but whose new c4     ours' c4 ..." in combined
                    is, base's and the other side's c3 are taken as c4
                    (their serves with it), and that side's c3 is its
                    addition
    a cherry-pick   theirs' c3 that ours has under another id it added  _renumber; "item c4: theirs' item c3 is
    then a merge    (same item, same view) takes that id, not a new     ours' c4 ..."; both versions differing:
                    one: the two copies are one item                    the conflict "item:c4"

diff(old, new) reports changes with the same identities (netlist instances also "inst:M1:props:W", "inst:M1:pins:G").
Reordering is no change, except the order of the netlist's ports: it is the subcircuit's pin order ("ports").
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

import yaml

from undine.design import intent
from undine.schematics import netlist
from undine.vcs.merge3 import MISSING, Merge, Result, describe, mapping, order

SUFFIXES = {"netlist": netlist.SUFFIX, "intent": intent.SUFFIX}
_PER_KEY = {"props": "property", "pins": "pin"}    # netlist record fields merged key by key
_NUMBERED = re.compile(r"^(.*?)(\d+)$")
_UID = "uid:"                                     # an item's identity when it has a uid (intent.new_uid)

Item = tuple[str | None, dict]                     # (view, or None for the cell intent; the item)


def kind_of(path: str | Path) -> str | None:
    """"netlist" or "intent" for a file name of that view, else None."""
    for kind, suffix in SUFFIXES.items():
        if str(path).endswith(suffix):
            return kind
    return None


def _kind(kind: str | None, path: str | Path | None) -> str:
    k = kind or (kind_of(path) if path is not None else None)
    if k not in SUFFIXES:
        raise ValueError(f"not a YAML view: {kind or path!r} (netlist or intent)")
    return k


def merge(base: bytes | None, ours: bytes, theirs: bytes, prefer: dict[str, str] | None = None,
          kind: str | None = None, path: str | Path | None = None, **kw) -> Result:
    """Merges a netlist or intent file; `kind` names the view, or `path` does by its suffix."""
    k = _kind(kind, path)
    return (merge_netlist if k == "netlist" else merge_intent)(base, ours, theirs, prefer)


def diff(old: bytes | None, new: bytes | None, kind: str | None = None, path: str | Path | None = None,
         **kw) -> dict:
    k = _kind(kind, path)
    return (diff_netlist if k == "netlist" else diff_intent)(old, new)


# ---------------------------------------------------------------- reading


def _yaml(data: bytes) -> Any:
    text = data.decode(errors="replace")
    try:
        return yaml.safe_load(text) if text.strip() else None
    except yaml.YAMLError as e:
        raise ValueError(f"not valid YAML: {e}") from e


def read_netlist(data: bytes | None) -> dict:
    """netlist.load of a file's bytes (None: no file, an empty netlist); ValueError when it is not a netlist."""
    if data is None:
        return netlist.empty()
    raw = _yaml(data) or {}
    if not isinstance(raw, dict):
        raise ValueError("a netlist is a mapping (ports, instances, code, pending)")
    shape = "a netlist has a list of ports, a mapping of instances, a list of code and a mapping pending"
    if not isinstance(raw.get("pending") or {}, dict):          # what netlist.normalize reads as no pending
        raise ValueError(shape)
    m = netlist.normalize(raw)
    if not (isinstance(m["instances"], dict) and all(isinstance(r, dict) for r in m["instances"].values())
            and all(isinstance(m[k], list) and all(isinstance(e, dict) for e in m[k]) for k in ("ports", "code"))):
        raise ValueError(shape)
    return m


def read_intent(data: bytes | None) -> dict:
    """intent.load of a file's bytes (None or an empty file: an empty intent); ValueError when it is not one."""
    if data is None:
        return intent.empty()
    try:
        return intent.normalize(_yaml(data) or {})
    except TypeError as e:                         # an id that is no text or number (id: [c, 3])
        raise ValueError(f"not an intent file: {e}") from e


def _intent_text(model: dict) -> str:
    return "" if intent.is_empty(model) else intent.dump(model)


def _written(text: str, ours_text: str, theirs_text: str, ours: bytes, theirs: bytes) -> bytes:
    """The merged file: a side's own bytes when the merged model is that side's (no formatting churn)."""
    if text == ours_text:
        return ours
    if text == theirs_text:
        return theirs
    return text.encode()


def _unreadable(m: Merge, kind: str, ours: bytes, theirs: bytes, err: Exception) -> Result:
    """A side that is not a file of its view has no objects to merge: one conflict for the file, ours whole."""
    side = m.conflict("file", f"the {kind} file cannot be read ({err})", None, None, None)
    return m.result(theirs if side == "theirs" else ours, kind)


# ---------------------------------------------------------------- netlist


def _named(entries: list[dict]) -> dict[str, dict]:
    """Ports or code blocks by name; a repeated name is told apart by its occurrence (s1#2)."""
    out: dict[str, dict] = {}
    for e in entries:
        name = str(e.get("name", ""))
        key, n = name, 1
        while key in out:
            n += 1
            key = f"{name}#{n}"
        out[key] = e
    return out


def _record(m: Merge, cid: str, what: str, b: Any, o: dict, t: dict) -> dict:
    """An instance or code block: props and pins per key, every other field (symbol, name) one value."""
    b = b if isinstance(b, dict) else {}
    out = {}
    for f in dict.fromkeys([*o, *t, *b]):
        bv, ov, tv = b.get(f, MISSING), o.get(f, MISSING), t.get(f, MISSING)
        if f in _PER_KEY and all(isinstance(v, dict) or v is MISSING for v in (bv, ov, tv)):
            v = mapping(m, f"{cid}:{f}:", f"{what}: {_PER_KEY[f]} ", bv or {}, ov or {}, tv or {})
        else:
            v = m.value(f"{cid}:{f}", f"{what}: {f}", bv, ov, tv)
        if v is not MISSING:
            out[f] = v
    return out


def _pin(model: dict, key: str) -> Any:
    """The net of pin "inst.pin" in a netlist; MISSING when it has no such instance or pin."""
    inst, _, pin = key.rpartition(".")
    pins = model["instances"].get(inst, {}).get("pins")
    return pins.get(pin, MISSING) if isinstance(pins, dict) else MISSING


def _lone(model: dict, key: str) -> bool:
    """Whether pin "inst.pin" is on a lone net: unnamed (netlist._AUTO), no other pin on it, not a port. That is a pin
    left floating, as NetlistEditor tells it."""
    net = _pin(model, key)
    if net is MISSING or not netlist._AUTO.match(str(net)) or any(p.get("name") == net for p in model["ports"]):
        return False
    return sum(n == net for r in model["instances"].values() if isinstance(r.get("pins"), dict)
               for n in r["pins"].values()) == 1


class _Gone(str):
    """A pin whose instance a side removed or renamed: reading the merged drawing drops a pending connection on it."""


def _on(model: dict, net: Any) -> set[str]:
    """The pins drawn to a net (not pending)."""
    return {f"{i}.{p}" for i, r in model["instances"].items() if isinstance(r.get("pins"), dict)
            for p, n in r["pins"].items() if n == net and f"{i}.{p}" not in model["pending"]}


def _drawn(side: dict, base: dict, key: str) -> Any:
    """The net a side drew a pin to, else MISSING: the pin's net when the side has no pending connection on it and the
    net is not a lone one; a _Gone when the side removed (or renamed) the pin's instance. A pin on base's net is drawn
    by the side only when base did not show it drawn there (pending or floating), or, for an unnamed net, when the side
    drew more pins to it: read_drawing names a new unnamed net after the old one it shares the most pins with, so a pin
    wired up keeps its netN."""
    net = _pin(side, key)
    if net is MISSING:
        return _Gone(f"{key.rpartition('.')[0]} removed") if _pin(base, key) is not MISSING else MISSING
    if key in side["pending"] or _lone(side, key):
        return MISSING
    if net == _pin(base, key) and key not in base["pending"] and not _lone(base, key) \
            and (not netlist._AUTO.match(str(net)) or _on(side, net) <= _on(base, net)):
        return MISSING                             # base drew it there already: not this side's drawing
    return net


def _shown(d: Any) -> str:
    return str(d) if isinstance(d, _Gone) else f"drawn to {d}"


def _pending(m: Merge, b: dict, o: dict, t: dict) -> tuple[dict[str, str], dict[str, Any]]:
    """Pending connections per pin, and the net each such pin gets in the merged instances: the pending net, or for a
    conflict decided for a side without one, that side's net. A pin one side connects (adds or moves its pending
    net) while the other draws it to another net is a conflict too: the merged drawing shows the pin on the drawn
    net, so reading it would drop the pending connection without a word. The drawn side is shown "drawn to <net>"."""
    out: dict[str, str] = {}
    pins: dict[str, Any] = {}
    for key in dict.fromkeys([*b["pending"], *o["pending"], *t["pending"]]):
        bv, ov, tv = (x["pending"].get(key, MISSING) for x in (b, o, t))
        cid, what = f"pending:{key}", f"pending connection {key}"
        od, td = _drawn(o, b, key), _drawn(t, b, key)
        n = len(m.conflicts)
        if ov is not MISSING and ov != bv and tv is MISSING and td not in (MISSING, ov):
            v = ov if m.conflict(cid, what, bv, ov, _shown(td)) == "ours" else MISSING
        elif tv is not MISSING and tv != bv and ov is MISSING and od not in (MISSING, tv):
            v = tv if m.conflict(cid, what, bv, _shown(od), tv) == "theirs" else MISSING
        else:
            v = m.value(cid, what, bv, ov, tv)
        if v is not MISSING:
            out[key] = pins[key] = v
        elif len(m.conflicts) > n:                 # decided for the side without a pending connection: its pin
            if (net := _pin(o if m.conflicts[-1].taken == "ours" else t, key)) is not MISSING:
                pins[key] = net
    return out, pins


def merge_netlist(base: bytes | None, ours: bytes, theirs: bytes, prefer: dict[str, str] | None = None) -> Result:
    m = Merge(prefer)
    if ours == theirs:
        return m.result(ours, "netlist")
    try:
        b, o, t = read_netlist(base), read_netlist(ours), read_netlist(theirs)
    except ValueError as e:
        return _unreadable(m, "netlist", ours, theirs, e)
    d = Merge()            # the parts the drawing decides: their conflicts keep ours and are not reported

    def record(prefix: str, words: str) -> Callable[[str, Any, Any, Any], Any]:
        return lambda k, bv, ov, tv: _record(d, f"{prefix}{k}", f"{words}{k}", bv, ov, tv)

    out = netlist.empty()
    out["ports"] = list(mapping(d, "port:", "port ", _named(b["ports"]), _named(o["ports"]),
                                _named(t["ports"])).values())
    out["instances"] = mapping(d, "inst:", "instance ", b["instances"], o["instances"], t["instances"],
                               record("inst:", "instance "))
    out["code"] = list(mapping(d, "code:", "code ", _named(b["code"]), _named(o["code"]), _named(t["code"]),
                               record("code:", "code ")).values())
    out["pending"], pins = _pending(m, b, o, t)
    for key, net in pins.items():
        inst, _, pin = key.rpartition(".")
        rec = out["instances"].get(inst)
        if net is not MISSING and isinstance(rec, dict) and isinstance(rec.get("pins"), dict) \
                and rec["pins"].get(pin, net) != net:
            out["instances"][inst] = {**rec, "pins": {**rec["pins"], pin: net}}
    m.combined += d.combined
    # a pin the pending merge decided (pending in the result, or a pending conflict) is not warned of; any other pin
    # the sides drew differently is the drawing's (the warning), even where a side had it pending before
    decided = {f"inst:{k.rpartition('.')[0]}:pins:{k.rpartition('.')[2]}" for k in pins}
    derived = [c for c in d.conflicts if c.id not in decided]
    if derived:
        names = ", ".join(c.what for c in derived[:6]) + (" ..." if len(derived) > 6 else "")
        m.warn(f"both sides changed the netlist's {names} differently: ours is kept; ports, instances and code "
               "follow the merged schematic the next time Undine reads it")
    text = netlist.dump(out)
    return m.result(_written(text, netlist.dump(o), netlist.dump(t), ours, theirs), "netlist")


# ---------------------------------------------------------------- intent


def _merge_lines(base: str, ours: str, theirs: str) -> str | None:
    """git's line merge of three texts; None when lines conflict or git cannot run."""
    with tempfile.TemporaryDirectory(prefix="undine-merge-") as d:
        files = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            (Path(d) / name).write_bytes(text.encode())
            files.append(str(Path(d) / name))
        try:
            r = subprocess.run(["git", "merge-file", "-p", "-q", *files], capture_output=True, timeout=30,
                               stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
    return r.stdout.decode(errors="replace") if r.returncode == 0 else None


def _notes(m: Merge, b: str, o: str, t: str) -> str:
    if o == t or t == b:
        return o
    if o == b:
        m.combined.append("notes")
        return t
    merged = _merge_lines(b, o, t)
    if merged is not None:
        m.combined.append("notes: the lines both sides changed")
        return merged
    return t if m.conflict("notes", "notes (both sides changed the same lines)", b, o, t) == "theirs" else o


def _items(model: dict) -> dict[str, Item]:
    """Items by identity: their uid (intent.new_uid), or for an item made without one, the text of its id (id: 7 and
    id: '7' are one id, as intent.entries keys them). serves are taken as identities too (_keyed)."""
    out: dict[str, Item] = {}
    for view, it in intent.all_items(model):
        k = f"{_UID}{it['uid']}" if it.get("uid") else str(it["id"])
        out[k if k not in out else str(it["id"])] = (view, it)     # a uid copied by hand: the item by its id
    return _keyed(out)


def _keyed(x: dict[str, Item]) -> dict[str, Item]:
    """The items with serves naming identities instead of ids (an id no item has stays as it is)."""
    ids = {str(it["id"]): k for k, (_, it) in x.items()}
    return {k: (view, {**it, "serves": [ids.get(str(s), s) for s in it["serves"]]} if _ids(it.get("serves")) else it)
            for k, (view, it) in x.items()}


def _legacy(k: str) -> bool:
    """An item without a uid, known by its id alone."""
    return not k.startswith(_UID)


def _ident(view: str | None, item_id: str) -> tuple[str, str]:
    """The identity and the words of an item: item:c1 / view:layout:item:c3."""
    if view is None:
        return f"item:{item_id}", f"item {item_id}"
    return f"view:{view}:item:{item_id}", f"{view} view item {item_id}"


def _show_item(v: Any) -> Any:
    if v is MISSING:
        return None
    view, it = v
    return describe(it if view is None else {"view": view, **it})


def _ids(v: Any) -> bool:
    """A list of item ids: strings, or numbers (intent.normalize takes id: 7)."""
    return isinstance(v, list) and all(isinstance(s, (str, int)) and not isinstance(s, bool) for s in v)


def _rekey(x: dict[str, Item], i: str, j: Any) -> dict[str, Item]:
    """A side's items with item i taken as id j (in place in the order) and every reference to i (serves) with it."""
    out: dict[str, Item] = {}
    for k, (view, it) in x.items():
        if _ids(it.get("serves")) and i in map(str, it["serves"]):
            it = {**it, "serves": [j if str(s) == i else s for s in it["serves"]]}
        if k == i:
            k, it = str(j), {**it, "id": j}
        out[k] = (view, it)
    return out


def _follow(m: Merge, b: dict[str, Item], o: dict[str, Item], t: dict[str, Item]
            ) -> tuple[dict[str, Item], dict[str, Item], dict[str, Item]]:
    """Follows a renumbering an earlier merge made (_renumber), on either side. Branch a merged b, both having added a
    c3: a has its own c3 and b's as c4, b still calls its item c3. Merging b again (base: b's old tip, where c3 is b's
    item) by id would put b's edits of its c3 on a's c3. So for a base id i both sides have, where one side's item i
    is not base's item i (_same_item) but the other side's is, and the one side has an id j that base and the other
    side lack whose item is base's item i: base's and the other side's item i are taken as j, and their references
    to i (serves) with it. The one side's own i is then its addition. Returns base, ours and theirs."""
    for side_is_ours in (True, False):
        side, other = (o, t) if side_is_ours else (t, o)
        moves: dict[str, Any] = {}
        for i, (_, bi) in b.items():
            if not _legacy(i) or i not in side or i not in other or _same_item(side[i][1], bi) \
                    or not _same_item(other[i][1], bi):
                continue
            j = next((j for j, (_, it) in side.items() if _legacy(j) and j not in b and j not in other
                      and j not in map(str, moves.values()) and _same_item(it, bi)), None)
            if j is not None:
                moves[i] = side[j][1]["id"]
        for i, j in moves.items():
            b, other = _rekey(b, i, j), _rekey(other, i, j)
            who, whose = ("theirs", "ours") if side_is_ours else ("ours", "theirs")
            m.combined.append(f"item {j}: {who}' item {i} is {whose}' {j} (an earlier merge renumbered it), and "
                              f"{whose}' {i} is {whose}' own; references in {who}' items follow")
        o, t = (o, other) if side_is_ours else (other, t)
    return b, o, t


def _renumber(m: Merge, b: dict[str, Item], o: dict[str, Item], t: dict[str, Item]) -> dict[str, Item]:
    """Theirs' items whose id ours added for another item, with the id after the highest of the same prefix on any
    side (like intent.next_id, never a gap: a gap is an id some branch may still have); the serves of the items
    theirs added or changed follow the new ids. When ours added the same item (_same_item, in the same view) under
    another id j, which theirs does not have for another item (or moves away too), theirs' item takes j instead: an
    earlier cherry-pick or merge renumbered it on ours, and a new id would make it two items."""
    moved = [i for i, (_, it) in t.items() if _legacy(i) and i not in b and i in o and o[i] != t[i]
             and not _same_item(o[i][1], it)]
    used = set(b) | set(o) | set(t)
    new: dict[str, Any] = {}                       # theirs' id -> the id it takes (ours' copy's id, or a new one)
    copies: set[str] = set()
    for i in moved:
        view, it = t[i]
        j = next((j for j, (v, x) in o.items() if _legacy(j) and j not in b and (j not in t or j in moved)
                  and j not in map(str, new.values()) and v == view and _same_item(x, it)), None)
        if j is not None:
            new[i] = o[j][1]["id"]
            copies.add(i)
            continue
        hit = _NUMBERED.match(str(i))                  # an id need not be a string (id: 7): its text, like next_id
        prefix = hit.group(1) if hit else intent.ID_PREFIX.get(it.get("kind"), "i")
        n = 1 + max((int(h.group(2)) for u in used if (h := _NUMBERED.match(str(u))) and h.group(1) == prefix),
                    default=0)
        new[i] = f"{prefix}{n}"
        used.add(new[i])
    if not new:
        return t
    out: dict[str, Item] = {}
    for i, (view, it) in t.items():
        if b.get(i) != (view, it) and _ids(it.get("serves")) and set(map(str, it["serves"])) & set(new):
            it = {**it, "serves": [new.get(str(s), s) for s in it["serves"]]}
        if i in new:
            it = {**it, "id": new[i]}
        out[str(new.get(i, i))] = (view, it)
    for i, j in new.items():
        if i in copies:
            m.combined.append(f"item {j}: theirs' item {i} is ours' {j} (an earlier cherry-pick or merge renumbered "
                              f"it), merged as one; serves in theirs' items follow")
        else:
            m.combined.append(f"item {j}: theirs' item {i} (both sides added a different {i}), renumbered; "
                              f"serves in theirs' items follow")
    return out


def _measured(x: dict) -> tuple | None:
    """What a spec measures: its measure, on its testbench and output; None without a measure."""
    spec = x.get("spec")
    if not isinstance(spec, dict) or spec.get("measure") is None:
        return None
    return spec["measure"], spec.get("testbench"), spec.get("output")


def _same_item(a: dict, b: dict) -> bool:
    """Whether two items under one id, or under two ids (_follow, _renumber), are one item (a cherry-picked addition
    edited on one side, an item an earlier merge renumbered): the same kind, and the same text, the same whole rule,
    the same spec measure on the same testbench and output, or the same source naming the request it came from. A
    source counts only with more than who added it (request, turn): IntentEditor.add_item sets {by: user} on every
    item a person adds. Merged as one item then, not renumbered."""
    if a.get("kind") != b.get("kind"):
        return False
    src = a.get("source")
    return (a.get("text") is not None and a.get("text") == b.get("text")
            or a.get("rule") is not None and a.get("rule") == b.get("rule")
            or _measured(a) is not None and _measured(a) == _measured(b)
            or isinstance(src, dict) and bool(set(src) - {"by"}) and src == b.get("source"))


def _serves(m: Merge, what: str, b: list, o: list, t: list) -> list:
    """serves as a set: what either side added or removed (a set member has no conflict)."""
    keep = set()
    for s in dict.fromkeys([*b, *o, *t]):
        inb, ino, intt = s in b, s in o, s in t
        if ino == inb and intt != inb:
            m.combined.append(f"{what}: serves {s} {'added' if intt else 'removed'}")
        if intt if ino == inb else ino:
            keep.add(s)
    return order(b, o, t, keep)


def _item(m: Merge, k: str, b: Item, o: Item, t: Item) -> Item:
    """An item all three sides have: its view one value, each field one value, serves a set. Its id is a name, not its
    identity when it has a uid: a side that renamed it (an earlier merge renumbered it) wins, ours when both did."""
    ids = [x[1]["id"] for x in (b, o, t)]
    name = ids[1] if ids[1] != ids[0] or _legacy(k) else ids[2]
    view = m.value(f"item:{name}:view", f"item {name}: view", b[0], o[0], t[0])
    cid, what = _ident(view, name)
    out = {"id": name}
    for f in intent.ITEM_FIELDS[1:]:
        bv, ov, tv = b[1].get(f, MISSING), o[1].get(f, MISSING), t[1].get(f, MISSING)
        if f == "serves" and all(_ids(v) or v is MISSING for v in (bv, ov, tv)):
            v = _serves(m, what, bv or [], ov or [], tv or [])
        else:
            v = m.value(f"{cid}:{f}", f"{what}: {f}", bv, ov, tv)
        if v is not MISSING and v != []:
            out[f] = v
    return view, out


def _merge_items(m: Merge, b: dict[str, Item], o: dict[str, Item], t: dict[str, Item]) -> dict[str, Item]:
    res: dict[str, Item] = {}
    for k in dict.fromkeys([*b, *o, *t]):
        bv, ov, tv = b.get(k, MISSING), o.get(k, MISSING), t.get(k, MISSING)
        if bv is not MISSING and ov is not MISSING and tv is not MISSING:
            v = _item(m, k, bv, ov, tv)
        else:                          # added, deleted, or deleted on one side and changed on the other
            view, it = next(x for x in (ov, tv, bv) if x is not MISSING)
            cid, what = _ident(view, it["id"])
            v = m.value(cid, what, bv, ov, tv, _show_item)
        if v is not MISSING:
            res[k] = v
    return {k: res[k] for k in order(b, o, t, set(res))}


def _unique_ids(m: Merge, items: dict[str, Item], o: dict[str, Item], used: set[str]) -> None:
    """Two merged items with one id (both sides added a c3, each with its own uid): the one ours has under that id
    keeps it, the others take the id after the highest of the prefix anywhere (serves name identities here, so they
    follow by themselves)."""
    by: dict[str, list[str]] = {}
    for k, (_, it) in items.items():
        by.setdefault(str(it["id"]), []).append(k)
    for name, keys in by.items():
        if len(keys) < 2:
            continue
        keep = next((k for k in keys if k in o and str(o[k][1]["id"]) == name), keys[0])
        for k in keys:
            if k == keep:
                continue
            view, it = items[k]
            hit = _NUMBERED.match(name)
            prefix = hit.group(1) if hit else intent.ID_PREFIX.get(it.get("kind"), "i")
            n = 1 + max((int(h.group(2)) for u in used if (h := _NUMBERED.match(u)) and h.group(1) == prefix), default=0)
            items[k] = (view, {**it, "id": f"{prefix}{n}"})
            used.add(f"{prefix}{n}")
            m.combined.append(f"item {prefix}{n}: theirs' item {name} (both sides added a different {name}), "
                              f"renumbered")


def _with_ids(items: dict[str, Item]) -> dict[str, Item]:
    """The merged items with serves naming ids again."""
    ids = {k: it["id"] for k, (_, it) in items.items()}
    return {k: (view, {**it, "serves": [ids.get(s, s) for s in it["serves"]]} if _ids(it.get("serves")) else it)
            for k, (view, it) in items.items()}


def merge_intent(base: bytes | None, ours: bytes, theirs: bytes, prefer: dict[str, str] | None = None) -> Result:
    m = Merge(prefer)
    if ours == theirs:
        return m.result(ours, "intent")
    try:
        b, o, t = read_intent(base), read_intent(ours), read_intent(theirs)
    except ValueError as e:
        return _unreadable(m, "intent", ours, theirs, e)

    def port(name: str, bv: Any, ov: Any, tv: Any) -> Any:
        if all(isinstance(v, dict) or v is MISSING for v in (bv, ov, tv)):
            return mapping(m, f"port:{name}:", f"port {name}: ", bv or {}, ov, tv) or MISSING
        return m.value(f"port:{name}", f"port {name}", bv, ov, tv)

    out = intent.empty()
    out["notes"] = _notes(m, b["notes"], o["notes"], t["notes"])
    out["ports"] = mapping(m, "port:", "port ", b["ports"], o["ports"], t["ports"], port)
    out["supplies"] = mapping(m, "supply:", "supply ", b["supplies"], o["supplies"], t["supplies"])
    bi, oi, ti = _follow(m, _items(b), _items(o), _items(t))
    ti = _renumber(m, bi, oi, ti)
    items = _merge_items(m, bi, oi, ti)
    _unique_ids(m, items, oi, {str(it["id"]) for x in (bi, oi, ti, items) for _, it in x.values()})
    for view, it in items.values():        # a reference the merge broke: one side deleted what the other now serves
        serves = it.get("serves") if _ids(it.get("serves")) else []
        gone = [(oi.get(s) or ti[s])[1]["id"] for s in serves if s not in items and (s in oi or s in ti)]
        if gone:
            m.warn(f"{_ident(view, it['id'])[1]} serves {', '.join(map(str, gone))}, which the merged intent does "
                   "not have")
    items = _with_ids(items)
    out["items"] = [it for view, it in items.values() if view is None]
    for view in order(b["views"], o["views"], t["views"], {view for view, _ in items.values() if view is not None}):
        out["views"][view] = {"items": [it for v, it in items.values() if v == view]}
    text = _intent_text(out)
    return m.result(_written(text, _intent_text(o), _intent_text(t), ours, theirs), "intent")


# ---------------------------------------------------------------- diff


def _change(out: list[dict], cid: str, what: str, before: Any, after: Any) -> None:
    do = "add" if before is MISSING else "del" if after is MISSING else "mod"
    out.append({"do": do, "id": cid, "what": what, "before": describe(before), "after": describe(after)})


def _diff_map(out: list[dict], prefix: str, what: str, a: dict, b: dict,
              sub: Callable[[list[dict], str, str, Any, Any], None] | None = None) -> None:
    """Changes of a mapping by key, in the new order with deleted keys where they were; `sub` details a key both
    have (else its value is one value)."""
    for k in order(a, b, a, set(a) | set(b)):
        x, y = a.get(k, MISSING), b.get(k, MISSING)
        if x == y:
            continue
        if sub is not None and x is not MISSING and y is not MISSING:
            sub(out, f"{prefix}{k}", f"{what}{k}", x, y)
        else:
            _change(out, f"{prefix}{k}", f"{what}{k}", x, y)


def _diff_fields(out: list[dict], cid: str, what: str, x: Any, y: Any) -> None:
    if isinstance(x, dict) and isinstance(y, dict):
        _diff_map(out, f"{cid}:", f"{what}: ", x, y)
    else:
        _change(out, cid, what, x, y)


def _diff_record(out: list[dict], cid: str, what: str, x: dict, y: dict) -> None:
    for f in order(x, y, x, set(x) | set(y)):
        xv, yv = x.get(f, MISSING), y.get(f, MISSING)
        if f in _PER_KEY and all(isinstance(v, dict) or v is MISSING for v in (xv, yv)):
            _diff_map(out, f"{cid}:{f}:", f"{what}: {_PER_KEY[f]} ", xv or {}, yv or {})
        elif xv != yv:
            _change(out, f"{cid}:{f}", f"{what}: {f}", xv, yv)


def diff_netlist(old: bytes | None, new: bytes | None) -> dict:
    a, b = read_netlist(old), read_netlist(new)
    out: list[dict] = []
    pa, pb = _named(a["ports"]), _named(b["ports"])
    _diff_map(out, "port:", "port ", pa, pb)
    if [k for k in pa if k in pb] != [k for k in pb if k in pa]:
        _change(out, "ports", "port order (the subcircuit's pin order)", list(pa), list(pb))
    _diff_map(out, "inst:", "instance ", a["instances"], b["instances"], _diff_record)
    _diff_map(out, "code:", "code ", _named(a["code"]), _named(b["code"]), _diff_record)
    _diff_map(out, "pending:", "pending connection ", a["pending"], b["pending"])
    return {"kind": "netlist", "changes": out}


def diff_intent(old: bytes | None, new: bytes | None) -> dict:
    a, b = read_intent(old), read_intent(new)
    out: list[dict] = []
    if a["notes"] != b["notes"]:
        _change(out, "notes", "notes", a["notes"] or MISSING, b["notes"] or MISSING)
    _diff_map(out, "port:", "port ", a["ports"], b["ports"], _diff_fields)
    _diff_map(out, "supply:", "supply ", a["supplies"], b["supplies"])
    ia, ib = _items(a), _items(b)
    for i in order(ia, ib, ia, set(ia) | set(ib)):
        x, y = ia.get(i, MISSING), ib.get(i, MISSING)
        if x == y:
            continue
        # the change's id is the item's identity (its uid); its words, the id a person reads
        if x is MISSING or y is MISSING:
            view, it = y if x is MISSING else x
            what = _ident(view, it["id"])[1] + (f" ({it['kind']}): {it['text']}" if it.get("text") else "")
            out.append({"do": "add" if x is MISSING else "del", "id": _ident(view, i)[0], "what": what,
                        "before": _show_item(x), "after": _show_item(y)})
            continue
        if x[0] != y[0]:
            _change(out, f"item:{i}:view", f"item {y[1]['id']}: view", x[0], y[0])
        cid, what = _ident(y[0], i)[0], _ident(y[0], y[1]["id"])[1]
        for f in intent.ITEM_FIELDS[1:]:
            xv, yv = x[1].get(f, MISSING), y[1].get(f, MISSING)
            if f == "serves" and _ids(xv) and _ids(yv) and set(xv) == set(yv):
                continue                                   # a set: reordering is no change
            if xv != yv:
                _change(out, f"{cid}:{f}", f"{what}: {f}", xv, yv)
    return {"kind": "intent", "changes": out}
