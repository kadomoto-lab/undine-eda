"""Schematic connectivity and netlisting (server side).

Connectivity is extracted from the Xschem files (wires, pins, net labels) into the cell's netlist view
(netlist.py), which the netlisters, LVS and schematic-driven layout read; `extract` here is the drawing's own
connectivity (wires and their nets), for the editor and cross-probing. SPICE output follows the symbols' `format` /
`lvs_format` attributes like Xschem does; a second backend turns the same
graph into a VLSIR package for `vlsirtools` (SPICE / Xyce / Spectre dialects).
"""
from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from undine import _core

from undine.schematics import xschem
from undine.store import cas
from undine.schematics.xschem import XComp, XFile

PIN_LAYER = 5
NAMING_TYPES = {"label", "ipin", "opin", "iopin"}
PORT_TYPES = {"ipin", "opin", "iopin"}
SKIP_TYPES = NAMING_TYPES | {"noconn", "probe", "show_label", "launcher", "arch_declarations",
                              "architecture", "attributes", "package", "port_attributes", "netlist_options",
                              "bus_connect", "bus_tap", "generic", "param"}
CODE_TYPES = {"netlist_commands", "netlist_options"}


@dataclass
class SymPin:
    name: str
    x: float
    y: float
    dir: str = "inout"


@dataclass
class Device:
    name: str
    comp: XComp
    index: int                       # position in the schematic's comp list
    sym: XFile | None
    symname: str
    type: str
    pins: list[tuple[str, float, float]] = field(default_factory=list)  # name, abs x, abs y
    nets: dict[str, str] = field(default_factory=dict)                  # pin -> net name


@dataclass
class Connectivity:
    cell: str
    devices: list[Device]
    nets: dict[str, list[tuple[str, str]]]     # net -> [(instance, pin)]
    ports: list[tuple[str, str]]               # (name, dir) in file order
    wire_net: list[str]                        # net name per wire index
    wire_ids: list[str]                        # stable wire ids (same order)
    net_points: dict[str, list[tuple[float, float]]]
    code: list[tuple[XComp, XFile]]            # netlist_commands blocks
    warnings: list[str]


# ------------------------------------------------------------------ helpers


def sym_pins(sym: XFile) -> list[SymPin]:
    out = []
    for layer, x1, y1, x2, y2, pr in sym.boxes:
        if layer == PIN_LAYER:
            out.append(SymPin(pr.get("name", ""), (x1 + x2) / 2, (y1 + y2) / 2, pr.get("dir", "inout")))
    return out


def sym_template(sym: XFile | None) -> dict[str, str]:
    return xschem.parse_props(sym.global_props.get("template", "")) if sym else {}


def inst_props(comp: XComp, sym: XFile | None) -> dict[str, str]:
    p = dict(sym_template(sym))
    p.update(comp.props)
    return p


def _on_segment(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> bool:
    eps = 1e-6
    if min(x1, x2) - eps <= px <= max(x1, x2) + eps and min(y1, y2) - eps <= py <= max(y1, y2) + eps:
        return abs((x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)) < eps * max(1.0, abs(x2 - x1) + abs(y2 - y1))
    return False


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a: int, b: int) -> None:
        self.p[self.find(a)] = self.find(b)


def resolve_symbol(resolver: xschem.SymbolResolver, comp: XComp, base: Path) -> XFile | None:
    return comp.embedded or resolver.load(comp.sym, base)


# -------------------------------------------------------------- extraction


def extract(path: Path, resolver: xschem.SymbolResolver, cell: str | None = None) -> Connectivity:
    """Connectivity of one schematic (undine._core, Rust; _py_extract is the reference it must match)."""
    return _core.extract(str(path), [str(p) for p in resolver.paths], cell, XFile, XComp, xschem.XText,
                               Device, Connectivity)


def _py_extract(path: Path, resolver: xschem.SymbolResolver, cell: str | None = None) -> Connectivity:
    f = xschem.parse(path.read_text(errors="replace"))
    base = path.parent
    warnings: list[str] = []
    devices: list[Device] = []
    labels: list[tuple[str, float, float]] = []       # net-naming pins: (name, x, y)
    ports: list[tuple[str, str]] = []
    code: list[tuple[XComp, XFile]] = []
    nodes: list[tuple[float, float]] = []             # union-find nodes: wires first, then pins
    for w in f.wires:
        nodes.append((w[0], w[1]))
    nwires = len(f.wires)

    pin_nodes: list[tuple[int, int, str]] = []        # (node idx, device idx or -1-label idx, pin name)

    ids = xschem.comp_ids(f)
    for i, c in enumerate(f.comps):
        sym = resolve_symbol(resolver, c, base)
        if sym is None:
            warnings.append(f"missing symbol {c.sym} (instance {c.props.get('name', '?')})")
            continue
        typ = sym.global_props.get("type", "")
        props = inst_props(c, sym)
        absp = []
        for p in sym_pins(sym):
            rx, ry = xschem._rot(c.rot, c.flip, p.x, p.y)
            absp.append((p.name, rx + c.x, ry + c.y))
        if typ in CODE_TYPES:
            code.append((c, sym))
            continue
        if typ in NAMING_TYPES:
            lab = props.get("lab", "")
            if typ in PORT_TYPES:
                ports.append((lab, typ))
            if absp and lab:
                labels.append((lab, absp[0][1], absp[0][2]))
            continue
        if typ in SKIP_TYPES:
            continue
        if props.get("spice_ignore", "") == "true" and not props.get("lvs_ignore"):
            pass
        d = Device(ids[i], c, i, sym, Path(c.sym).stem, typ, absp)
        devices.append(d)

    # nodes for pins and labels
    all_pts: list[tuple[str, int, float, float]] = []  # kind, owner, x, y
    for di, d in enumerate(devices):
        for pn, x, y in d.pins:
            all_pts.append((f"d:{pn}", di, x, y))
    for li, (lab, x, y) in enumerate(labels):
        all_pts.append(("l", li, x, y))
    base_idx = nwires
    for k, (_, _, x, y) in enumerate(all_pts):
        nodes.append((x, y))
    uf = _UF(len(nodes))

    # wires touching wires (endpoint on segment)
    for a in range(nwires):
        ax1, ay1, ax2, ay2, _ = f.wires[a]
        for b in range(a + 1, nwires):
            bx1, by1, bx2, by2, _ = f.wires[b]
            if (_on_segment(ax1, ay1, bx1, by1, bx2, by2) or _on_segment(ax2, ay2, bx1, by1, bx2, by2)
                    or _on_segment(bx1, by1, ax1, ay1, ax2, ay2) or _on_segment(bx2, by2, ax1, ay1, ax2, ay2)):
                uf.union(a, b)
    # pins / labels onto wires, and onto other pins at the same point
    by_point: dict[tuple[float, float], int] = {}
    for k, (kind, owner, x, y) in enumerate(all_pts):
        node = base_idx + k
        for w in range(nwires):
            x1, y1, x2, y2, _ = f.wires[w]
            if _on_segment(x, y, x1, y1, x2, y2):
                uf.union(node, w)
        key = (round(x, 4), round(y, 4))
        if key in by_point:
            uf.union(node, by_point[key])
        else:
            by_point[key] = node

    # names
    root_names: dict[int, list[str]] = {}
    for w, wire in enumerate(f.wires):
        lab = wire[4].get("lab", "")
        if lab:
            root_names.setdefault(uf.find(w), []).append(lab)
    for k, (kind, owner, x, y) in enumerate(all_pts):
        if kind == "l":
            root_names.setdefault(uf.find(base_idx + k), []).append(labels[owner][0])
    # nets sharing a name are the same net
    name_root: dict[str, int] = {}
    for r, names in list(root_names.items()):
        for n in names:
            if n in name_root:
                uf.union(r, name_root[n])
            else:
                name_root[n] = r
    final_names: dict[int, str] = {}
    for r, names in root_names.items():
        final_names.setdefault(uf.find(r), None)
    pool: dict[int, list[str]] = {}
    for r, names in root_names.items():
        pool.setdefault(uf.find(r), []).extend(names)
    for root, names in pool.items():
        # a net named by a port label wins, else the first name
        port_names = [n for n in names if any(n == p[0] for p in ports)]
        final_names[root] = (port_names or names)[0]
    counter = 0

    def net_of(node: int) -> str:
        nonlocal counter
        r = uf.find(node)
        if r not in final_names or final_names[r] is None:
            counter += 1
            final_names[r] = f"net{counter}"
        return final_names[r]

    nets: dict[str, list[tuple[str, str]]] = {}
    net_points: dict[str, list[tuple[float, float]]] = {}
    wire_net = [net_of(w) for w in range(nwires)]
    for w, nm in enumerate(wire_net):
        net_points.setdefault(nm, []).extend([(f.wires[w][0], f.wires[w][1]), (f.wires[w][2], f.wires[w][3])])
    for k, (kind, owner, x, y) in enumerate(all_pts):
        nm = net_of(base_idx + k)
        if kind.startswith("d:"):
            d = devices[owner]
            pn = kind[2:]
            d.nets[pn] = nm
            nets.setdefault(nm, []).append((d.name, pn))
        net_points.setdefault(nm, []).append((x, y))
    for nm in set(wire_net) | set(net_points):
        nets.setdefault(nm, [])
    wire_ids = [xschem.wire_id(*w[:4]) for w in f.wires]
    return Connectivity(cell or path.stem, devices, nets, ports, wire_net, wire_ids, net_points, code, warnings)


# ---------------------------------------------------------------- netlisting

# a backslash ends a parameter name and makes the next character literal, as in Xschem: @prefix\and4_1 (sky130's standard
# cells), $[@model\] (IHP's resistors), v(@@p\) (devices/.save)
_FMT_TOKEN = re.compile(r"\\(.)|@@([A-Za-z0-9_\[\]\.]+)|@([A-Za-z_][A-Za-z0-9_]*)", re.S)


def _fmt(fmt: str, props: dict[str, str], dev: Device, extra: dict[str, str]) -> str:
    pinlist = " ".join(dev.nets.get(pn, "0") for pn, _, _ in dev.pins)

    def rep(m: re.Match) -> str:
        if m.group(1) is not None:
            return m.group(1)
        if m.group(2):
            return dev.nets.get(m.group(2), "")
        k = m.group(3)
        if k == "pinlist":
            return pinlist
        if k == "symname":
            return dev.symname
        if k in extra:
            return extra[k]
        return _value(props.get(k, ""), props, extra)

    return _FMT_TOKEN.sub(rep, fmt)


# Xschem property values may refer to other parameters (@W) and compute with expr(...), as the sky130 MOS symbols do
# since open_pdks 2024: ad="expr('int((@nf + 1)/2) * @W / @nf * 0.29')". Xschem substitutes the parameters, then
# evaluates the expression in floating point; when an operand is not a number (a .param name), the expression goes to
# the simulator as {...} instead.
_SI_SCALE = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15, "a": 1e-18}
_NUMBER = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)(meg|[tgkmunpfa])?[a-zA-Z]*\s*$", re.I)
_PARAM = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")
_SUFFIXED = re.compile(r"(?<![\w.])(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?(?:meg|[tgkmunpfa])?(?![\w.])", re.I)   # a number, maybe 2u
_FUNCS = {"int": lambda v: float(int(v)), "abs": abs, "min": min, "max": max, "round": round, "sqrt": math.sqrt,
          "floor": math.floor, "ceil": math.ceil, "pow": pow, "exp": math.exp, "log": math.log, "log10": math.log10}


def _number(text: str) -> float | None:
    m = _NUMBER.match(str(text))
    if not m:
        return None
    return float(m.group(1)) * _SI_SCALE.get((m.group(2) or "").lower(), 1.0)


def _arith(node) -> float:
    if isinstance(node, ast.Expression):
        return _arith(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _arith(node.operand)
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp):
        a, b = _arith(node.left), _arith(node.right)
        ops = {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b, ast.Div: lambda: a / b,
               ast.Pow: lambda: a ** b, ast.Mod: lambda: math.fmod(a, b)}
        if type(node.op) in ops:
            return ops[type(node.op)]()
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return float(_FUNCS[node.func.id](*[_arith(x) for x in node.args]))
    raise ValueError("not plain arithmetic")


_EXPR = re.compile(r"expr(?:_eng)?\(")


def _expr_spans(text: str) -> list[tuple[int, int, str]]:
    """(start, end, inner) of each expr(...) / expr_eng(...) in text (IHP's resistors give their value with expr_eng,
    which Xschem prints in engineering notation: the same number), with balanced parentheses and an optional '...'
    quote."""
    out, i = [], 0
    while (m := _EXPR.search(text, i)) is not None:
        i = m.start()
        depth, j = 0, m.end() - 1
        while j < len(text):
            depth += {"(": 1, ")": -1}.get(text[j], 0)
            if depth == 0:
                break
            j += 1
        if depth:
            break
        inner = text[m.end():j].strip()
        if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
            inner = inner[1:-1]
        out.append((i, j + 1, inner))
        i = j + 1
    return out


def _value(value: str, props: dict[str, str], extra: dict[str, str], depth: int = 0) -> str:
    """A property value as netlisted: @parameters inside it substituted, expr(...) evaluated."""
    if "@" not in value and not _EXPR.search(value) or depth > 4:
        return value

    def param(m: re.Match) -> str:
        k = m.group(1)
        v = extra.get(k, props.get(k, ""))
        return _value(v, props, extra, depth + 1) if v else m.group(0)

    spans = _expr_spans(value)
    if not spans:
        return _PARAM.sub(param, value)
    out, last = [], 0
    for start, end, inner in spans:
        out.append(_PARAM.sub(param, value[last:start]))
        text = _PARAM.sub(param, inner)
        numeric = re.sub(r"[A-Za-z_][A-Za-z0-9_]*\s*\(", "(", text)          # function names aside,
        words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _SUFFIXED.sub("0", numeric))  # what is left must be numbers
        try:
            if words:
                raise ValueError(words)
            v = _arith(ast.parse(_SUFFIXED.sub(lambda m: repr(_number(m.group(0))), text), mode="eval"))
            out.append(str(int(v)) if v == int(v) and abs(v) < 1e15 else f"{v:.12g}")
        except (ValueError, SyntaxError, ZeroDivisionError, TypeError, OverflowError):
            out.append("{" + text + "}")                                    # the simulator evaluates it
        last = end
    out.append(_PARAM.sub(param, value[last:]))
    return "".join(out)


def _tidy(s: str) -> str:
    out = []
    for line in s.splitlines():
        t = line.rstrip()
        if t.strip():
            out.append(t)
    return "\n".join(out)


def _subst_vars(text: str, env: dict[str, str]) -> str:
    for k, v in env.items():
        text = text.replace("$::" + k, v).replace("${" + k + "}", v).replace("$" + k, v)
    return text


class SameNameError(ValueError):
    """spice() met two cells of one name with other content in one netlist (one subcircuit per name)."""


def _marker(d: Path) -> dict | None:
    """The install marker of directory `d` (a dependency's install under .undine/deps), or None: no marker, or one
    that is not a JSON object. Read through cas, not deps, so that netlisting does not import the dependency code."""
    try:
        m = json.loads((d / cas.MARKER).read_text())
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def owner(path: Path) -> str:
    """Who a netlisted file belongs to, for messages: "analog_ip 1.2.0 (ip)" for a package installed under local name
    ip, "ip (git 229914698b1f)" for a dependency without a manifest, else the name of the file's directory (a project
    or PDK library). Never raises: a message about a netlist must not fail on a broken marker."""
    path = Path(path)
    m = _marker(path.parent)
    if m is None:
        return path.parent.name
    local = m.get("name") if isinstance(m.get("name"), str) and m.get("name") else path.parent.name
    if isinstance(m.get("package"), str) and m["package"]:
        version = m.get("version")
        label = f"{m['package']} {version}" if isinstance(version, str) and version else m["package"]
        return f"{label} ({local})"
    commit = m.get("commit")
    return f"{local} (git {commit[:12]})" if isinstance(commit, str) and commit else local


def _side(path: Path, cell: str) -> str:
    """One side of a SameNameError: "inv in analog_ip 1.2.0 (ip)" for an installed dependency, "lib/inv" (as before
    markers existed) for a plain directory."""
    return f"{cell} in {owner(path)}" if _marker(path.parent) is not None else f"{path.parent.name}/{cell}"


@dataclass
class Source:
    """Where a group of netlist lines came from, for the trust gate: the schematic (or its netlist view) whose cell
    body the text is written into (`home`), and the symbol whose format and template formed it (`symbol`; None for a
    code block written on the instance). Both matter: a trusted symbol's @value is filled from the placing schematic,
    and a dependency's symbol can be placed in a project cell. kind is 'code' (a code block), 'device' (an instance's
    line), 'header' (a code block placed before the top cell), 'subckt' (a .subckt line: the cell's name and the pins
    of the symbol it is formed for) or 'generated' (Undine's own corner .lib line; home and symbol None). `cell` is the cell whose body it is in, `name` the instance's or code block's name."""
    home: Path | None
    symbol: Path | None
    cell: str
    kind: str
    name: str
    text: str


@dataclass
class NetlistResult:
    text: str
    ports: list[str]
    warnings: list[str]
    subckts: list[str]
    extracted: list[str] = field(default_factory=list)    # subcircuits taken from a layout extraction (spice extracted)
    substituted: list[str] = field(default_factory=list)  # the cells replaced by their stand-ins (spice substitute)
    instances: dict[str, str] = field(default_factory=dict)   # the top cell's elements: {as written (M1, MXMS): instance}
    sources: list[Source] = field(default_factory=list)   # where each group of lines in the text came from
    files: set[Path] = field(default_factory=set)         # every .sch, .netlist.yaml and .sym read (resolved paths)


@dataclass
class Standin:
    """A cell netlisted in place of another at every depth (spice `substitute`): which cell it replaces and which it is
    ("lib/cell", for messages), its cell name (the subcircuit's name), its symbol (the instances' format and template;
    None: the original's, with the stand-in's name as @symname), its schematic (the subcircuit; None: only what the
    symbol's format writes) and its file (the schematic, or the symbol without one: what tells it from another cell of
    its name)."""
    replaces: str
    name: str
    cell: str
    sym: XFile | None
    sch: Path | None
    file: Path


def pins_differ(a: list[str], b: list[str]) -> str | None:
    """How pin list `b` differs from `a` (names in order: a subcircuit's ports), in words; None when they are the same."""
    if a == b:
        return None
    gone, new = [p for p in a if p not in b], [p for p in b if p not in a]
    if gone or new:
        return "; ".join([*([f"without {' '.join(gone)}"] if gone else []), *([f"with {' '.join(new)}"] if new else [])])
    if len(a) != len(b):
        return "a pin twice"
    k = next(i for i, (x, y) in enumerate(zip(a, b)) if x != y)
    return f"in another order: pin {k + 1} is {b[k]}, not {a[k]}"


_LVS_PREFIX = {"nmos": "M", "pmos": "M", "npn": "Q", "pnp": "Q", "diode": "D"}


def flatten_spice(text: str, top: str) -> str:
    """Inlines every subcircuit call (x...) below `top`; what a flat layout extraction is compared with."""
    subs: dict[str, tuple[list[str], list[str]]] = {}
    cur: tuple[str, list[str], list[str]] | None = None
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("*"):
            continue
        low = ln.lower()
        if low.startswith(".subckt"):
            t = ln.split()
            cur = (t[1], [x for x in t[2:] if "=" not in x], [])
        elif low.startswith(".ends"):
            if cur:
                subs[cur[0]] = (cur[1], cur[2])
            cur = None
        elif cur and not ln.startswith("."):
            cur[2].append(ln)
    if top not in subs:
        raise KeyError(f"no subcircuit {top} in the netlist")

    def expand(name: str, nets: dict[str, str], prefix: str, stack: tuple[str, ...]) -> list[str]:
        out = []
        for ln in subs[name][1]:
            t = ln.split()
            if t[0][:1].lower() == "x":
                args = [x for x in t[1:] if "=" not in x]
                callee = args[-1]
                if callee not in subs:
                    # a PDK device written as a call to its model (GF180's resistors: XR1 a b sub ppolyf_u r_width=..):
                    # a leaf, only its nets are renamed
                    nodes = [nets.get(x, f"{prefix}{x}") if x != "0" else "0" for x in args[:-1]]
                    rest = t[1 + len(args):]
                    out.append(" ".join([f"{t[0]}.{prefix[:-1]}" if prefix else t[0], *nodes, callee, *rest]))
                    continue
                if callee in stack:
                    raise ValueError(f"recursive subcircuit {callee}")
                ports = subs[callee][0]
                out += expand(callee, {p: nets.get(n, f"{prefix}{n}") for p, n in zip(ports, args[:-1])}, f"{prefix}{t[0]}.", stack + (callee,))
                continue
            nn = 4 if t[0][:1].upper() == "M" else 3 if t[0][:1].upper() in "QJ" else 2
            nodes = [nets.get(n, f"{prefix}{n}") if n != "0" else "0" for n in t[1:1 + nn]]
            out.append(" ".join([f"{t[0]}.{prefix[:-1]}" if prefix else t[0], *nodes, *t[1 + nn:]]))   # the first letter keeps the element type
        return out

    ports = subs[top][0]
    body = expand(top, {p: p for p in ports}, "", (top,))
    return "\n".join([f".subckt {top} " + " ".join(ports), *body, ".ends", ".end", ""])


def connectivity(path: Path, resolver: xschem.SymbolResolver, cell: str | None = None,
                 source: str = "netlist") -> Connectivity:
    """What a consumer reads: the cell's netlist view (pending connections included), or with source "drawing" the
    drawing's own connectivity (only drawn connections)."""
    if source == "drawing":
        return extract(path, resolver, cell)
    from undine.schematics import netlist
    return netlist.connectivity(path, resolver, cell, write=False)


def spice(path: Path, resolver: xschem.SymbolResolver, env: dict[str, str] | None = None,
          mode: str = "spice", top_subckt: bool | None = None, top_name: str | None = None,
          source: str = "netlist", overrides: dict[str, dict[str, dict[str, str]]] | None = None,
          extracted: dict[Path, str] | None = None, substitute: dict[Path, Standin] | None = None) -> NetlistResult:
    """SPICE netlist of a schematic and the hierarchy below it, from the cells' netlist views. `overrides`
    ({schematic cell: {instance: {property: value}}}) netlists instances with other property values, as if they had
    been edited (derived values such as a transistor's ad / as follow), for sweeps that never write the design.
    `extracted` ({schematic file: subcircuit extracted from its layout}) puts that subcircuit, with its ports in the
    symbol's pin order, in place of the schematic and everything below it. `substitute` ({the original's symbol file:
    its Standin}) netlists every instance of a cell, at any depth, as its stand-in (a behavioural model of the same
    pins, checked by the caller: pins_differ); ValueError when a stand-in contains the cell it replaces (at any depth,
    through other stand-ins too) or has the name of another cell netlisted here (one subcircuit per name), and when
    two schematics of one cell name (two libraries' cells: the project's and a dependency's, or two dependencies')
    would give the netlist subcircuits of other content under that one name. Two of the same content are one."""
    env = env or {}
    warnings: list[str] = []
    subckts: dict[str, list[str]] = {}
    from_layout = {Path(k).resolve(): v for k, v in (extracted or {}).items()}
    used: list[str] = []
    standins = {Path(k).resolve(): v for k, v in (substitute or {}).items()}
    replaced = {p.stem for p in standins}                 # a symbol of another name is never one of them
    swapped: list[str] = []
    instances: dict[str, str] = {}
    names: dict[str, tuple[Path, Standin | None]] = {}    # with stand-ins: the cell behind each subcircuit name
    via: dict[str, Standin] = {}                          # the cells the netlister entered as stand-ins
    homes: dict[str, Path] = {}                           # the schematic each subcircuit name was netlisted from
    inside: dict[str, Path] = {}                          # the schematic of each cell on the stack
    alike: set[Path] = set()                              # schematics found to netlist as the one of their name
    sources: list[Source] = []
    files: set[Path] = set()
    found: dict[tuple[str, Path], Path | None] = {}       # resolver.find, once per (symbol, directory)

    def sym_file(name: str, base: Path) -> Path | None:
        """The resolved .sym an instance or code block of `base` names, recorded as read."""
        if (name, base) not in found:
            f = resolver.find(name, base)
            found[(name, base)] = f.resolve() if f is not None else None
        if (f := found[(name, base)]) is not None:
            files.add(f)
        return f

    def net(n: str) -> str:
        return "0" if mode not in ("lvs", "netgen") and n.upper() == "GND" else n

    def standin_check(d: Device, cell: str, stand: Standin | None, child: Path | None, base: Path,
                      stack: tuple[str, ...]) -> None:
        """With stand-ins, before subcircuit `d` (in `cell`) is called: its name is one cell's (a stand-in of another
        cell's name would share that cell's subcircuit), and the netlister is not inside it (a stand-in that contains
        the cell it replaces, at any depth: the cycle passes through a stand-in, which a design does not have)."""
        home = stand.file if stand is not None else (child or resolver.find(d.comp.sym, base))
        if home is not None:
            home = home.resolve().with_suffix("")
            other, s = names.setdefault(d.symname, (home, stand))
            if other != home and (s := stand or s) is not None:
                raise ValueError(f"substitute {s.replaces} -> {s.name}: another cell named {s.cell} is netlisted in "
                                 f"this testbench as well ({(other if stand else home).parent.name}/{s.cell}), and a "
                                 "netlist has one subcircuit per name: choose a stand-in of a name of its own")
        if d.symname in stack:
            cyc = stack[stack.index(d.symname):]          # the cells from the one called down to `cell`, which calls it
            k = 0 if stand is not None else next((i for i in range(1, len(cyc)) if cyc[i] in via), None)
            if k is not None:                             # the stand-in, then round to the cell placing the original
                s = stand or via[cyc[k]]
                chain = " > ".join([*cyc[k:], *cyc[:k], s.replaces.partition("/")[2]])
                at = f", instance {d.name} in {cell}" if len(cyc) == 1 else ""
                raise ValueError(f"substitute {s.replaces} -> {s.name}: {s.name} contains {s.replaces} ({chain}{at}), "
                                 "the cell it replaces")

    def subckt(d: Device, child: Path, stand: Standin | None, stack: tuple[str, ...],
               formed: Path | None) -> list[str]:
        """Subcircuit d.symname from schematic `child` (or from its layout extraction), with the instance's pins.
        The .subckt line is formed from the symbol's pin names (`formed`, the .sym): a Source like any other line."""
        pins = [pn for pn, _, _ in d.pins]
        _one_word(d.symname, pins, f"symbol {formed or d.comp.sym}", "pin")
        if (text := from_layout.get(child.resolve())) is not None:
            if d.symname not in used:
                used.append(d.symname)
            return _extracted_subckt(text, d.symname, pins)
        if stand is not None:
            via[d.symname] = stand
        _, child_lines, _ = body(child, d.symname, stack + (d.symname,))
        head = f".subckt {d.symname} " + " ".join(pins)
        sources.append(Source(child.resolve(), formed, d.symname, "subckt", d.symname, head))
        return [head, *child_lines, ".ends"]

    def same_name_check(d: Device, cell: str, child: Path, stand: Standin | None, stack: tuple[str, ...],
                        formed: Path | None) -> None:
        """Subcircuit d.symname is netlisted from another schematic already. A netlist has one subcircuit per name, and
        Undine keeps the names as the cells' authors wrote them, so the first would silently stand for this one too:
        refused unless this one netlists to the same lines. Inside the first (it places a cell of its own name from
        another library) the two cannot be the same."""
        here = child.resolve()
        if here in alike or (d.symname in stack and inside.get(d.symname) == here):
            return                                        # compared already; a cell that places itself (as before)
        same = False
        if d.symname not in stack:
            # the comparison's Sources stay: when the two are the same, the one subcircuit is formed from both files
            n = len(warnings)                             # what the first said already is said once
            same = subckt(d, child, stand, stack, formed) == subckts[d.symname]
            warnings[n:] = [w for w in warnings[n:] if w not in warnings[:n]]
        if same:
            alike.add(here)
        else:
            first = homes[d.symname]
            raise SameNameError(f"{top_cell}: two cells named {d.symname} are netlisted, {_side(first, d.symname)} "
                             f"and {_side(here, d.symname)} (instance {d.name} in {cell}), and they differ: a "
                             "netlist has one subcircuit per name, so one would stand for the other. Rename one of "
                             "them, or place the same cell in both places")

    def body(p: Path, cell: str, stack: tuple[str, ...]) -> tuple[list[str], list[str], Connectivity]:
        inside[cell] = p.resolve()                        # a name is on the stack once (the recursion guard)
        try:
            return cell_lines(p, cell, stack)
        finally:
            inside.pop(cell, None)

    def cell_lines(p: Path, cell: str, stack: tuple[str, ...]) -> tuple[list[str], list[str], Connectivity]:
        conn = connectivity(p, resolver, cell, source)
        warnings.extend(f"{cell}: {w}" for w in conn.warnings)
        home = p.resolve()
        files.add(home)
        if (view := home.with_name(home.stem + ".netlist.yaml")).is_file():
            files.add(view)
        header, lines, tail = [], [], []
        heads: list[Source] = []                          # a child's header blocks are not written: kept for the top only
        for c, sym in conn.code:
            props = inst_props(c, sym)
            txt = _tidy(_subst_vars(props.get("value", ""), env))
            # the value is the symbol's when the block does not set its own (a template default)
            f = None if c.embedded is not None else sym_file(c.sym, p.parent)
            src = Source(home, None if "value" in c.props else f, cell, "code", props.get("name", ""), txt)
            fmt = (sym.global_props.get("format", "") if sym is not None else "") or ""
            if not txt and "tcleval(" in fmt:
                corners = env.get("UNDINE_CORNERS", "").split()
                if props.get("corner") and env.get("UNDINE_MODEL_LIB") and props["corner"] in corners:
                    # the PDK's corner symbol builds its .include lines in Tcl, which Undine does not run: the model
                    # library's section for that corner is the same set of models
                    txt = f".lib {env['UNDINE_MODEL_LIB']} {props['corner']}"
                    src = Source(None, None, cell, "generated", props.get("name", ""), txt)
                else:
                    warnings.append(f"{cell}: {props.get('name', 'a code block')} is written in Tcl (tcleval), which Undine "
                                    "does not evaluate: it adds nothing to the netlist")
            if txt:
                if props.get("place", "") == "header":
                    header.append(txt)
                    heads.append(src if src.kind == "generated" else replace(src, kind="header"))
                else:
                    tail.append(txt)
                    sources.append(src)
        if len(stack) == 1:
            sources.extend(heads)
        for d in conn.devices:
            stand = None
            own = sym_file(d.comp.sym, p.parent) if d.comp.embedded is None else None   # an embedded one is in `home`
            if d.symname in replaced and (f := own or resolver.find(d.comp.sym, p.parent)) is not None:
                stand = standins.get(f.resolve())
            if stand is not None:
                d.sym, d.symname = stand.sym or d.sym, stand.cell
                d.type = d.sym.global_props.get("type", "") if d.sym is not None else d.type
            props = inst_props(d.comp, d.sym)
            if (ov := (overrides or {}).get(p.stem, {}).get(d.name)):
                props = {**props, **ov}
            if props.get("lvs_ignore" if mode in ("lvs", "netgen") else "spice_ignore", "") == "true":
                continue
            if stand is not None and stand.replaces not in swapped:
                swapped.append(stand.replaces)
            d.nets = {pn: net(n) for pn, n in d.nets.items()}
            # a stand-in with a symbol of its own (api: <cell>.sym beside its file) forms the lines from that one
            formed = own if stand is None or stand.sym is None else stand.file.with_suffix(".sym").resolve()
            if d.type == "subcircuit":
                child = stand.sch if stand is not None else _find_sch(resolver, d.comp, p.parent)
                if standins:
                    standin_check(d, cell, stand, child, p.parent, stack)
                if child is not None and d.symname in homes and homes[d.symname] != child.resolve():
                    same_name_check(d, cell, child, stand, stack, formed)
                elif child is not None and d.symname not in subckts and d.symname not in stack:
                    homes[d.symname] = child.resolve()
                    subckts[d.symname] = []  # reserve (recursion guard)
                    subckts[d.symname] = subckt(d, child, stand, stack, formed)
            fmt = d.sym.global_props.get("lvs_format" if mode in ("lvs", "netgen") and "lvs_format" in d.sym.global_props
                                         else "format", "") if d.sym else ""
            if not fmt:
                warnings.append(f"{cell}: symbol {d.symname} has no netlist format")
                continue
            extra = {"savecurrent": ""}
            if mode == "lvs" and d.type in _LVS_PREFIX:
                # KLayout's SPICE reader builds device objects from M/Q/D elements, not X (subckt) calls
                pre = _LVS_PREFIX[d.type]
                nm = props.get("name", "")
                props = {**props, "spiceprefix": "" if nm[:1].upper() == pre else pre}
            lines.append(_tidy(_subst_vars(_fmt(fmt, props, d, extra), env)))
            if lines[-1]:
                if formed is not None:
                    files.add(formed)
                sources.append(Source(home, formed, cell, "device", d.name, lines[-1]))
            if len(stack) == 1 and lines[-1].split():
                instances[lines[-1].split()[0]] = d.name
        return header, lines + tail, conn

    top_cell = top_name or path.stem
    names[top_cell] = (path.resolve().with_suffix(""), None)
    homes[top_cell] = path.resolve()
    header, lines, conn = body(path, top_cell, (top_cell,))
    port_names = [n for n, _ in conn.ports]
    wrap = top_subckt if top_subckt is not None else bool(port_names)
    out = [f"** sch_path: {path}"]
    out += header
    if wrap:
        _one_word(top_cell, port_names, f"schematic {path}", "port")
        out.append(f".subckt {top_cell} " + " ".join(port_names))
        sources.append(Source(path.resolve(), None, top_cell, "subckt", top_cell, out[-1]))
    out += [l for l in lines if l]
    if wrap:
        out.append(".ends")
    for body_lines in subckts.values():
        out += body_lines
    out.append(".end")
    return NetlistResult("\n".join(x for x in out if x) + "\n", port_names, warnings, list(subckts), used, swapped,
                         instances, sources=sources, files=files)


def _one_word(name: str, pins: list[str], where: str, what: str) -> None:
    """A .subckt line's name and pins are each one SPICE word: one holding a space or a line break would write cards
    of its own (a .control block, an .include) into the netlist. Refused when the netlist is formed."""
    for x in [name, *pins]:
        if any(ch.isspace() for ch in x):
            raise ValueError(f"{where}: {what if x != name else 'cell'} name {x!r} holds a space or a line break, which "
                             f"a netlist line cannot carry: rename it")


def _extracted_subckt(text: str, name: str, pins: list[str]) -> list[str]:
    """An extracted subcircuit (parasitics.py) as subcircuit `name` with its ports in the order of the symbol's pins;
    SPICE names are compared without case."""
    lines = text.splitlines()
    i = next((k for k, l in enumerate(lines) if l.lower().startswith(".subckt")), None)
    if i is None:
        raise ValueError(f"{name}: the extracted netlist has no .subckt")
    ports = lines[i].split()[2:]
    by = {p.lower(): p for p in ports}
    if sorted(by) != sorted(p.lower() for p in pins):
        raise ValueError(f"{name}: the extracted subcircuit's ports ({' '.join(ports)}) are not the symbol's pins "
                         f"({' '.join(pins)})")
    end = next((k for k in range(len(lines) - 1, i, -1) if lines[k].lower().startswith(".ends")), len(lines))
    return [*lines[:i], f".subckt {name} " + " ".join(by[p.lower()] for p in pins), *lines[i + 1:end], ".ends"]


def _find_sch(resolver: xschem.SymbolResolver, comp: XComp, base: Path) -> Path | None:
    sym = resolver.find(comp.sym, base)
    if sym is None:
        return None
    sch = sym.with_suffix(".sch")
    if sch.is_file():
        return sch
    nm = Path(comp.sym).with_suffix(".sch").name
    for pth in resolver.paths:
        cand = pth / nm
        if cand.is_file():
            return cand
    return None


# ---------------------------------------------------------------- VLSIR backend


def vlsir_package(path: Path, resolver: xschem.SymbolResolver, domain: str = "undine"):
    """Connectivity -> VLSIR Package (primitives become ExternalModules)."""
    import vlsir.circuit_pb2 as vc
    import vlsir.utils_pb2 as vu

    pkg = vc.Package(domain=domain)
    ext: dict[str, vc.ExternalModule] = {}
    modules: dict[str, vc.Module] = {}

    def num(v: str) -> vu.Param:
        try:
            return vu.Param(value=vu.ParamValue(double=float(_si(v))))
        except ValueError:
            return vu.Param(value=vu.ParamValue(literal=v))

    def build(p: Path, name: str, stack: tuple[str, ...]) -> vc.Module:
        if name in modules:
            return modules[name]
        conn = connectivity(p, resolver, name)
        m = vc.Module(name=name)
        modules[name] = m
        for pn, d in conn.ports:
            m.ports.append(vc.Port(signal=pn, direction={"ipin": vc.Port.INPUT, "opin": vc.Port.OUTPUT}.get(d, vc.Port.INOUT)))
        sigs = {n for n in conn.nets}
        for n in sorted(sigs):
            if n not in {pn for pn, _ in conn.ports}:
                m.signals.append(vc.Signal(name=n, width=1))
        for pn, _ in conn.ports:
            m.signals.append(vc.Signal(name=pn, width=1))
        for d in conn.devices:
            props = inst_props(d.comp, d.sym)
            inst = vc.Instance(name=d.name)
            if d.type == "subcircuit":
                child = _find_sch(resolver, d.comp, p.parent)
                if child is None:
                    continue
                build(child, d.symname, stack + (d.symname,))
                inst.module.CopyFrom(vu.Reference(local=d.symname))
            else:
                mdl = props.get("model") and f"sky130_fd_pr__{props['model']}" if "sky130" in str(d.comp.sym) else d.symname
                mdl = mdl or d.symname
                if mdl not in ext:
                    e = vc.ExternalModule(name=vu.QualifiedName(domain="", name=mdl))
                    for pn, _, _ in d.pins:
                        e.signals.append(vc.Signal(name=pn, width=1))
                        e.ports.append(vc.Port(signal=pn, direction=vc.Port.INOUT))
                    ext[mdl] = e
                    pkg.ext_modules.append(e)
                inst.module.CopyFrom(vu.Reference(external=vu.QualifiedName(domain="", name=mdl)))
                for k in ("L", "W", "nf", "mult", "m", "value"):
                    if props.get(k, "") != "":
                        inst.parameters.append(vu.Param(name=k, value=num(props[k]).value))
            for pn, _, _ in d.pins:
                inst.connections.append(vc.Connection(portname=pn, target=vc.ConnectionTarget(sig=d.nets.get(pn, "0"))))
            m.instances.append(inst)
        pkg.modules.append(m)
        return m

    top = build(path, path.stem, (path.stem,))
    pkg.domain = domain
    return pkg, top.name


_SI = {"f": "e-15", "p": "e-12", "n": "e-9", "u": "e-6", "m": "e-3", "k": "e3", "meg": "e6", "g": "e9", "t": "e12"}


def _si(v: str) -> str:
    s = v.strip().lower()
    m = re.fullmatch(r"([0-9.]+(?:e[-+]?[0-9]+)?)(meg|[fpnumkgt])?", s)
    if not m:
        raise ValueError(v)
    return m.group(1) + (_SI[m.group(2)] if m.group(2) else "")


def vlsirtools_netlist(path: Path, resolver: xschem.SymbolResolver, fmt: str = "spice") -> str:
    import io

    try:
        import vlsirtools
    except ImportError:
        raise RuntimeError("vlsirtools is not installed (optional): pixi install, or pip install vlsirtools") from None

    spice(path, resolver)       # vlsir_package keeps the first module of a name: two cells of one name are refused here
    pkg, top = vlsir_package(path, resolver)
    pkg.domain = "undine"
    buf = io.StringIO()
    vlsirtools.netlist(pkg=pkg, dest=buf, fmt=fmt)
    return buf.getvalue()


class XschemOrder(xschem.SymbolResolver):
    """Finds a symbol where Xschem does: on the search path first (XSCHEM_LIBRARY_PATH, first hit), in the placing
    schematic's directory only when the path has none, or for a ./ or ../ name. Undine's own netlister looks in that
    directory first (SymbolResolver.find), so a bare `inv.sym` placed from two libraries may be two cells there and one
    in Xschem."""

    def find(self, name: str, base: Path | None) -> Path | None:
        if base is not None and not name.startswith(("./", "../")):
            return super().find(name, None) or super().find(name, base)
        return super().find(name, base)


def xschem_netlist(path: Path, search_paths: list[Path], env_vars: dict[str, str]) -> str:
    """Runs the Xschem application headless (`xschem -n`) with the project's library paths."""
    import subprocess
    import tempfile

    from undine import tools

    # Xschem writes one subcircuit per name too: two cells of one name with other content are refused here, from
    # Undine's netlist of the same hierarchy as Xschem finds it. Only that: what else Undine's netlister would refuse,
    # Xschem's own run reports (or netlists), as before
    try:
        spice(path, XschemOrder(search_paths), env_vars)
    except SameNameError:
        raise
    except Exception:
        pass
    exe = tools.find("xschem")
    if not exe:
        raise RuntimeError("xschem not found (set UNDINE_TOOLS or put it on PATH)")
    with tempfile.TemporaryDirectory(prefix="undine-xschem-") as td:
        td_ = Path(td)
        rc = ["set XSCHEM_LIBRARY_PATH {}"]
        for p in search_paths:
            rc.append(f"append XSCHEM_LIBRARY_PATH :{p}")
        for k, v in env_vars.items():
            rc.append(f"set {k} {{{v}}}")
        (td_ / "xschemrc").write_text("\n".join(rc) + "\n")
        out = td_ / "out"
        out.mkdir()
        r = subprocess.run([exe, "-n", "-q", "--no_x", "--rcfile", str(td_ / "xschemrc"), "-o", str(out), str(path)],
                           capture_output=True, text=True, timeout=120, env=tools.env(), cwd=td)
        files = sorted(out.glob("*.spice"))
        if not files:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                               else "xschem produced no netlist")
        text = files[0].read_text()
    for k, v in env_vars.items():
        text = text.replace("$::" + k, v)
    return text
