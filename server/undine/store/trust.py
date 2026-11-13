"""What in a dependency's files would run as code, or read a file Undine never looked at, if Undine used it.

A dependency is someone else's work installed into a project. Its schematics become netlist lines that ngspice runs,
its RTL is compiled by Verilator and its symbols are read by Xschem, and each of these tools can do more than
describe a circuit:

    ngspice     a `.control` ... `.endc` block or a `*#` line runs commands (`shell` among them); `.include`, `.lib`
                and XSPICE `file=` read a file, which can be anything on the machine
    Verilator   `$c(...)` and `` `systemc_* `` paste C++ into the model; `$system(...)` runs a shell; DPI-C imports
                call C; `$fopen`, `$readmem*`, `$writemem*` and `` `include `` read or write files
    Xschem      `tcleval(...)` in an attribute is evaluated as Tcl when it netlists

The scanners here only find such content: what is allowed, and for whom, is the policy's (trusting a package is a
person's decision). They read only the text or the directory given, and they are conservative: whatever might be
code is reported, and a line they cannot read is reported as such ('unparsed') rather than passed.

    spice_findings(text, allowed=, env=, rules=)   a netlist's text: the lines one cell contributes, a value
    rtl_findings(text, full=)                      one RTL file
    tcl_findings(text)                             one .sch or .sym file
    scan_dir(d)                                    every finding in one installed library, by cell and file

The policy (the end of this file): a person trusts a dependency's version in a project (project.json "trusted":
["analog_ip@1.2.0"], or "ip@229914698b1f" for one without a manifest); everything installed under .undine/deps that
is not trusted is checked when a job is about to run, and refused with every finding named (UntrustedContent):

    check_netlist(result, untrusted=, ...)   ngspice (rules 'sim') at start_sim; KLayout and netgen (rules 'paths')
                                             when an LVS run launches
    check_tcl(untrusted)                     Xschem's own netlister, which evaluates tcleval( in any symbol it reads
    check_rtl(lib_dir, full=)                Verilator, on a dependency library's RTL (rtl.py), and LibreLane (its
                                             Verilator lint and Yosys) on a flow whose top is a dependency's
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import yaml

from undine.simulation import modellib

TEXT_MAX = 200                      # a finding quotes this much of its line


@dataclass(frozen=True)
class Finding:
    # control, control_line, path, unparsed (SPICE); rtl_c, systemc, rtl_system, dpi, rtl_path, rtl_define; tcl
    kind: str
    line: int                       # 1-based, in the text scanned (the first line of a card continued with +)
    text: str                       # that line or card, shortened
    path: str | None = None         # the file a path finding reads, as written
    where: str | None = None        # scan_dir: the file, and the instance and attribute the text is a value of


def _short(s: str) -> str:
    s = " ".join(s.split())
    return s if len(s) <= TEXT_MAX else s[:TEXT_MAX - 3] + "..."


def _line_at(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


# ------------------------------------------------------------------ SPICE

# an XSPICE model's file parameter; an unquoted value ends at the ) that closes the parameter list
_XSPICE_FILE = re.compile(r"(?i)\b(file|input_file|state_file)\s*=\s*(\"[^\"]*\"|[^\s)]+)")
_DOLLAR = "\x01"                    # stands for $ while modellib reads a card: it takes ' $' for a comment's start


def _cards(text: str) -> list[tuple[int, str]]:
    """(first line, card) for each card of a netlist text: a line and the `+` lines that continue it, joined as
    ngspice joins them. Comment lines (`*`) are cards of their own and do not break a continuation."""
    cards: list[list] = []
    last = None                     # the card a + line continues: the last one that is not a comment
    for n, line in enumerate(text.splitlines(), 1):
        s = line.lstrip()
        if s.startswith("+") and last is not None:
            last[1] += " " + s[1:]
            continue
        cards.append([n, line])
        if not s.startswith("*"):
            last = cards[-1]
    return [(n, c) for n, c in cards]


def _subst(path: str, env: dict) -> str:
    """The path as the netlister writes it: schematic._subst_vars (imported here, as schematic is a heavy module)."""
    from undine.schematics.schematic import _subst_vars
    return _subst_vars(path, env)


def _bad_path(path: str, allowed: list[Path], env: dict) -> bool:
    """A path that may read a file nobody scanned: still holding a variable or ~ after substitution, absolute
    outside every allowed root, or relative with a .. part (a relative one resolves in the job's directory)."""
    p = _subst(path, env)
    if "$" in p or "~" in p:
        return True
    if Path(p).is_absolute():
        r = Path(p).resolve()
        return not any(r.is_relative_to(a.resolve()) for a in allowed)
    return ".." in Path(p).parts


def _card_paths(n: int, card: str) -> tuple[list[str], Finding | None]:
    """The files one card reads (.include, .lib, XSPICE file=), or why they cannot be told."""
    paths = []
    if card.lstrip().lower().startswith((".inc", ".lib")):
        try:
            paths += [p.replace(_DOLLAR, "$") for p in modellib._includes(card.replace("$", _DOLLAR), f"line {n}")]
        except modellib.Unsupported:
            return [], Finding("unparsed", n, _short(card))
    for m in _XSPICE_FILE.finditer(card):
        v = m.group(2)
        paths.append(v[1:-1] if v.startswith('"') else v)
    return paths, None


def spice_findings(text: str, *, allowed: list[Path], env: dict,
                   rules: Literal["sim", "paths"] = "sim") -> list[Finding]:
    """What in netlist text would run commands or read an unscanned file. rules='sim' (ngspice) reports `.control`
    blocks and `*#` lines as well; rules='paths' (KLayout, netgen, which run no control code) reports paths only.
    `allowed` is the roots an absolute path may read (the PDK's); `env` the variables the netlister substitutes."""
    out: list[Finding] = []
    in_control = False
    for n, card in _cards(text):
        s = card.lstrip()
        word = s.split(maxsplit=1)[0].lower() if s else ""
        opens = word.startswith(".control")     # ngspice matches the card by prefix: .controlx and .control; open one
        if rules == "sim":
            if s.startswith("*#"):
                out.append(Finding("control_line", n, _short(card)))
            elif opens and not in_control:
                out.append(Finding("control", n, _short(card)))
            in_control = (in_control or opens) and not word.startswith(".endc")
        if s.startswith("*"):
            continue
        paths, unparsed = _card_paths(n, card)
        if unparsed:
            out.append(unparsed)
        out += [Finding("path", n, _short(card), p) for p in paths if _bad_path(p, allowed, env)]
    return out


# ------------------------------------------------------------------ RTL

# a string is kept as it is (a // in it is not a comment); a comment is blanked, newlines kept, so offsets stay put
_RTL_SKIP = re.compile(r'"(?:\\.|[^"\\\n])*"|//[^\n]*|/\*.*?\*/|/\*.*\Z', re.S)
# Verilator's $c, $c8 ... $c64 (its lexer: "$c"[0-9]*): not $clog2, $cast, $countones and the other system functions
# The name alone, '(' or not: a macro argument (`CALL($system, "...")) becomes the call once Verilator expands it.
_RTL_C = re.compile(r"\$c[0-9]*(?!\w)")
_SYSTEMC = re.compile(r"`systemc_\w+")
_SYSTEM = re.compile(r"\$system(?!\w)")
_DPI = re.compile(r'\b(?:import|export)\s+"DPI(?:-C)?"')
# a task whose first argument names a file it reads or writes ($dumpfile: the build has --trace, so a VCD is written)
_FILE_TASK = re.compile(r"\$(?:fopen|dumpfile|readmem\w*|writemem\w*)\s*\(\s*")
_DUMPPORTS = re.compile(r"\$dumpports\s*\(")    # ( scope, file ): the file comes after the scope list
_LITERAL = re.compile(r'"((?:\\.|[^"\\\n])*)"')
_INCLUDE = re.compile(r'`include\b[ \t]*(?:"([^"\n]*)"|<([^>\n]*)>)?')
_DEFINE = re.compile(r"`define\b((?:\\\r?\n|[^\n])*)")
# a body that holds the code, pastes it together, or builds a DPI import from parts (`define IMP import)
_DEFINE_CODE = re.compile(r"\$c[0-9]*(?!\w)|\$system\b|systemc_|``|\b(?:import|export)\b|DPI")


def _uncommented(text: str) -> str:
    return _RTL_SKIP.sub(lambda m: m.group(0) if m.group(0)[0] == '"' else re.sub(r"[^\n]", " ", m.group(0)), text)


def _top_args(s: str) -> list[str]:
    """A call's argument text split at the commas that are not inside parentheses or a string."""
    out, depth, cur, i = [], 0, [], 0
    while i < len(s):
        c = s[i]
        if c == '"':
            m = _LITERAL.match(s, i)
            if m:
                cur.append(m.group(0))
                i = m.end()
                continue
        depth += {"(": 1, "{": 1, ")": -1, "}": -1}.get(c, 0)
        if c == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    return out + ["".join(cur)]


def _rtl_bad_path(p: str) -> bool:
    return p.startswith(("/", "\\")) or bool(re.match(r"[A-Za-z]:", p)) or "~" in p or ".." in re.split(r"[\\/]", p)


def rtl_findings(text: str, *, full: bool = True) -> list[Finding]:
    """What in one RTL file would run as C++ or a shell, or read or write a file outside the sources, under
    Verilator. Comments are skipped. full=False (lint, which compiles nothing) reports `` `include `` paths only."""
    code = _uncommented(text)
    lines = text.splitlines()
    out: list[tuple[int, Finding]] = []

    def add(kind: str, pos: int, path: str | None = None) -> None:
        n = _line_at(code, pos)
        out.append((pos, Finding(kind, n, _short(lines[n - 1] if n <= len(lines) else ""), path)))

    for m in _INCLUDE.finditer(code):
        p = m.group(1) if m.group(1) is not None else m.group(2)
        if p is None or _rtl_bad_path(p):           # a macro (`include `FILE) names a file nobody can tell here
            add("rtl_path", m.start(), p)
    if not full:
        return [f for _, f in sorted(out, key=lambda x: x[0])]
    for kind, rx in (("rtl_c", _RTL_C), ("systemc", _SYSTEMC), ("rtl_system", _SYSTEM), ("dpi", _DPI)):
        for m in rx.finditer(code):
            add(kind, m.start())
    for m in _FILE_TASK.finditer(code):
        lit = _LITERAL.match(code, m.end())
        if lit is None or not re.match(r"\s*[,)]", code[lit.end():]) or _rtl_bad_path(lit.group(1)):
            add("rtl_path", m.start(), lit.group(1) if lit else None)
    for m in _DUMPPORTS.finditer(code):
        args = _top_args(_argument(code, m.end()))
        last = args[-1].strip() if len(args) > 1 else ""
        lit = re.fullmatch(r'"((?:\\.|[^"\\\n])*)"', last)
        if last and (lit is None or _rtl_bad_path(lit.group(1))):
            add("rtl_path", m.start(), lit.group(1) if lit else None)
    for m in _DEFINE.finditer(code):
        # a macro body can hold the code itself, or paste it together (`define C(x) $``x builds $c with no literal)
        if _DEFINE_CODE.search(m.group(1)):
            add("rtl_define", m.start())
    return [f for _, f in sorted(out, key=lambda x: x[0])]


# ------------------------------------------------------------------ Tcl

_TCLEVAL = re.compile(r"tcleval\s*\(")
_EXPR = re.compile(r"expr\s*\(")


def _argument(text: str, start: int) -> str:
    """The text from `start` (just after an opening parenthesis) to its closing one, or to the end."""
    depth = 1
    for i in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i]
    return text[start:]


def tcl_findings(text: str) -> list[Finding]:
    """What in a .sch or .sym file Xschem would evaluate as Tcl when it netlists: every `tcleval(`, and every
    `expr(` whose argument holds a command or variable substitution ([ or $). Xschem 3.4.5 leaves expr( as text;
    it is reported all the same, as later versions evaluate it."""
    lines = text.splitlines()
    # Xschem drops a value's backslash escapes before it looks for tcleval(, so t\\cleval( and tcl\eval( are
    # evaluated too: match with every backslash gone. No newline goes, so line numbers stay.
    scan = text.replace("\\", "")
    hits = [m.start() for m in _TCLEVAL.finditer(scan)]
    hits += [m.start() for m in _EXPR.finditer(scan) if re.search(r"[\[$]", _argument(scan, m.end()))]
    out = []
    for pos in sorted(hits):
        n = _line_at(scan, pos)
        out.append(Finding("tcl", n, _short(lines[n - 1] if n <= len(lines) else "")))
    return out


# ------------------------------------------------------------------ a library

RTL_SUFFIXES = (".sv", ".v", ".svh", ".vh")
_NETLIST = ".netlist.yaml"


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8", errors="replace")


def _values(where: str, props: dict) -> list[Finding]:
    """Every attribute value of an instance or symbol through spice_findings: which values become netlist text
    depends on the symbol's format, which a scan without a resolver cannot tell, so every one is read."""
    out = []
    for k, v in props.items():
        if v is None:
            continue
        for f in spice_findings(str(v), allowed=[], env={}):
            out.append(replace(f, where=f"{where} {k}"))
    return out


def _symbol(where: str, props: dict) -> list[Finding]:
    """A symbol's attributes, and each default its template gives an instance (a value= that becomes a card)."""
    from undine.schematics import xschem
    out = _values(where, props)
    if props.get("template"):
        out += _values(f"{where} template", xschem.parse_props(props["template"]))
    return out


def _sch(p: Path, text: str) -> list[Finding]:
    from undine.schematics import xschem
    f = xschem.parse(text)
    out = _values(p.name, f.global_props)
    if f.code.get("S"):
        out += [replace(x, where=f"{p.name} S block") for x in spice_findings(f.code["S"], allowed=[], env={})]
    for c in f.comps:
        out += _values(f"{p.name} {c.props.get('name') or c.sym}", c.props)
        if c.embedded is not None:                 # a symbol saved inside the schematic: its format and template
            out += _symbol(f"{p.name} {c.props.get('name') or c.sym} (embedded symbol)", c.embedded.global_props)
    return out


def _sym(p: Path, text: str) -> list[Finding]:
    from undine.schematics import xschem
    return _symbol(p.name, xschem.parse(text).global_props)


def _netlist(p: Path, text: str) -> list[Finding]:
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError("not a mapping")
    out = []
    for c in raw.get("code") or []:
        if isinstance(c, dict):
            out += _values(f"{p.name} code {c.get('name', '')}".rstrip(), c.get("props") or {})
    for name, inst in (raw.get("instances") or {}).items():
        if isinstance(inst, dict):
            out += _values(f"{p.name} {name}", inst.get("props") or {})
    return out


def _cell_of(name: str) -> str:
    return name[:-len(_NETLIST)] if name.endswith(_NETLIST) else name.rsplit(".", 1)[0]


def scan_dir(d: Path) -> dict:
    """Every finding in an installed library: its cells' schematics, symbols and netlists (every value read as SPICE,
    with no PDK root and no variables, so a $PDK_ROOT path is reported too), its RTL files at any depth and its
    .sch and .sym files as Tcl. Only what has findings is listed. The listing a person reads before trusting it."""
    scan: dict = {"cells": {}, "rtl": {}, "tcl": {}}
    readers = {".sch": _sch, ".sym": _sym}
    for p in sorted(x for x in d.iterdir() if x.is_file()):
        reader = _netlist if p.name.endswith(_NETLIST) else readers.get(p.suffix)
        if reader is None:
            continue
        text = _read(p)
        try:
            found = reader(p, text)
        except Exception as e:                     # what cannot be read cannot be passed
            found = [Finding("unparsed", 0, _short(f"cannot read {p.name}: {e}"), where=p.name)]
        if found:
            scan["cells"].setdefault(_cell_of(p.name), []).extend(found)
        if p.suffix in readers and (tcl := tcl_findings(text)):
            scan["tcl"][p.name] = [replace(f, where=p.name) for f in tcl]
    for p in sorted(x for x in d.rglob("*") if x.suffix in RTL_SUFFIXES and x.is_file()):
        rel = p.relative_to(d).as_posix()
        if found := rtl_findings(_read(p)):
            scan["rtl"][rel] = [replace(f, where=rel) for f in found]
    return scan


def counts(scan: dict) -> dict:
    """How many findings of each kind of content a scan_dir listing has: {'spice', 'rtl', 'tcl'}."""
    return {"spice": sum(map(len, scan["cells"].values())), "rtl": sum(map(len, scan["rtl"].values())),
            "tcl": sum(map(len, scan["tcl"].values()))}


# ------------------------------------------------------------------ the policy

# a trust entry of project.json: <package>@<version> (a package with a manifest), <name>@<commit12> (one without)
KEY = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*@[0-9A-Za-z][0-9A-Za-z.+-]*$")
LISTED = 10                         # a refusal's message quotes this many findings; the exception carries them all
_UNKNOWN = "unknown"                # the version part of a directory without an install marker


class UntrustedContent(PermissionError):
    """A job would run what an untrusted dependency's files say. `name` is the dependency's local name (its directory
    under .undine/deps), `package` its manifest's package name (None without one), `key` what a person trusts,
    `cell` the first finding's lib/cell and `findings` every finding, as (file, Finding) with file '<local>/<path>'.
    The first three are the first dependency's when several are refused; the message names each."""

    def __init__(self, message: str, *, name: str, package: str | None, key: str, cell: str,
                 findings: list[tuple[str, Finding]]):
        super().__init__(message)
        self.name, self.package, self.key, self.cell, self.findings = name, package, key, cell, findings


def _marker(d: Path) -> dict:
    from undine.store import cas
    try:
        m = json.loads((d / cas.MARKER).read_text())
    except (OSError, ValueError):
        return {}
    return m if isinstance(m, dict) else {}


def _identity(d: Path) -> tuple[str, str, str | None]:
    """(key, label for messages, package name) of an installed directory: its manifest's name and version, else its
    local name and the commit its marker records. A manifest that cannot be read names nothing: the commit does."""
    from undine.store import manifest
    try:
        man = manifest.from_dir(d, d.name)
    except (ValueError, OSError):                  # ManifestError is a ValueError
        man = None
    if man is not None:
        return f"{man.name}@{man.version}", f"{man.name} {man.version}", man.name
    commit = _marker(d).get("commit")
    ver = commit[:12] if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{12,64}", commit) else _UNKNOWN
    return f"{d.name}@{ver}", f"git {ver}" if ver != _UNKNOWN else "without an install record", None


def key_of(deps_dir: Path) -> str:
    """What a person trusts for the dependency installed in `deps_dir`: 'analog_ip@1.2.0' from its manifest, else
    '<local>@<commit12>' from its install marker."""
    return _identity(Path(deps_dir))[0]


def _trusted_in(project_json: Path) -> set[str]:
    try:
        meta = json.loads(project_json.read_text())
    except (OSError, ValueError):
        return set()
    t = meta.get("trusted") if isinstance(meta, dict) else None
    return {k for k in t if isinstance(k, str) and KEY.match(k)} if isinstance(t, list) else set()


def _real_project(project_dir: Path) -> Path | None:
    """The project a changeset's worktree (<root>/.changesets/<project>/<cid>/work[/prefix], or base) belongs to."""
    parts = Path(project_dir).parts
    for i in range(len(parts) - 4, -1, -1):
        if parts[i] == ".changesets" and parts[i + 3] in ("work", "base"):
            return Path(*parts[:i]) / parts[i + 1]
    return None


def trusted_keys(project_dir: Path) -> set[str]:
    """The keys project.json trusts (entries that are not keys are left out; status reports them). In a changeset's
    worktree the real project's only: a person trusts (and untrusts) in the project, and that reaches an agent's open
    changeset as it is now. The worktree's own project.json is a copy from when it opened, so it grants nothing."""
    if (real := _real_project(project_dir)) is not None:
        return _trusted_in(real / "project.json")
    return _trusted_in(Path(project_dir) / "project.json")


def untrusted_dirs(project_dir: Path) -> dict[Path, tuple[str, str]]:
    """{directory: (local name, key)} of every directory under .undine/deps whose key the project does not trust:
    leftovers and staging ones too, as each resolves on the symbol path all the same. deps._deps_root refuses a
    .undine or .undine/deps that is a link."""
    from undine.store import deps
    try:
        root = deps._deps_root(Path(project_dir))
    except deps.DepError:
        if _declares_git(Path(project_dir)):
            raise
        return {}                                   # a linked .undine with nothing declared to install: no dependency
    if not root.is_dir():
        return {}
    trusted = trusted_keys(project_dir)
    out = {}
    for d in sorted(root.iterdir()):
        if d.is_dir() and (key := key_of(d)) not in trusted:
            out[d] = (d.name, key)
    return out


def _declares_git(project_dir: Path) -> bool:
    """Whether project.json (or, in a changeset's worktree, the real project's) names any git dependency."""
    paths = [project_dir / "project.json"]
    if (real := _real_project(project_dir)) is not None:
        paths.append(real / "project.json")
    for p in paths:
        try:
            libs = json.loads(p.read_text()).get("libraries")
        except (OSError, ValueError, AttributeError):
            continue
        specs = libs.values() if isinstance(libs, dict) else libs if isinstance(libs, list) else []
        if any(isinstance(v, dict) and "git" in v for v in specs):
            return True
    return False


def _project_name(d: Path) -> str:
    """The name of the project dependency directory `d` (<project>/.undine/deps/<local>) is installed in."""
    try:
        name = json.loads((d.parent.parent.parent / "project.json").read_text()).get("name")
    except (OSError, ValueError, AttributeError):
        name = None
    return name if isinstance(name, str) and name else "PROJECT"


def _refuse(groups: dict[Path, dict]) -> None:
    """Raises UntrustedContent for `groups` ({dependency directory: {local, key, label, package, items: [(file,
    cell, what, Finding)]}}, in the order found) when any has items."""
    groups = {d: g for d, g in groups.items() if g["items"]}
    if not groups:
        return
    said = []
    for g in groups.values():
        for _, cell, what, f in g["items"]:
            at = " ".join(x for x in (what, f"line {f.line}" if f.line else "") if x)
            said.append(f"{cell} (dependency {g['label']}, not trusted): {at + ': ' if at else ''}{f.text}")
    if len(said) > LISTED:
        said[LISTED:] = [f"and {len(said) - LISTED} more"]
    hints = []
    for d, g in groups.items():
        prj = _project_name(d)
        if g["key"].endswith("@" + _UNKNOWN) or not KEY.match(g["key"]):
            hints.append(f"{g['local']} was not installed by Undine: undine lib sync {prj} removes or reinstalls it")
        else:
            hints.append(f"a person can trust {g['key']} in this project (undine lib trust {prj} {g['local']}, or "
                         "Trust... in Undine)")
    first = next(iter(groups.values()))
    raise UntrustedContent("; ".join(said + hints) + "; an agent cannot",
                           name=first["local"], package=first["package"], key=first["key"],
                           cell=first["items"][0][1],
                           findings=[(file, f) for g in groups.values() for file, _, _, f in g["items"]])


def _group(d: Path, local: str, key: str) -> dict:
    _, label, package = _identity(d)
    return {"local": local, "key": key, "label": label, "package": package, "items": []}


def _within(f, dirs: dict[Path, tuple[str, str]]) -> Path | None:
    if f is None:
        return None
    f = Path(f)
    return next((d for d in dirs if f.is_relative_to(d)), None)


def check_netlist(result, *, untrusted: dict[Path, tuple[str, str]], pdk_root: Path | None, env: dict,
                  rules: Literal["sim", "paths"]) -> None:
    """Refuses (UntrustedContent) a netlist (schematic.NetlistResult) whose lines from an untrusted dependency hold
    what spice_findings reports under `rules`: lines written into a dependency cell's body (home) and lines its
    symbol formed in any cell (a project's instance of it, a template default). The PDK's own root is the one an
    absolute path may read; `env` is what the netlister substituted. Undine's generated corner line is not checked."""
    if not untrusted:
        return
    dirs = {Path(d).resolve(): v for d, v in untrusted.items()}
    allowed = [Path(pdk_root)] if pdk_root else []
    groups: dict[Path, dict] = {}
    for s in result.sources:
        if s.kind == "generated":
            continue
        found = None
        for f in dict.fromkeys(x for x in (s.home, s.symbol) if x is not None):
            d = _within(f, dirs)
            if d is None or (f == s.symbol and d == _within(s.home, dirs)):
                continue                           # one finding per dependency: the body's when both are its
            if found is None:
                found = spice_findings(s.text, allowed=allowed, env=env, rules=rules)
            if not found:
                break
            local, key = dirs[d]
            rel = Path(f).relative_to(d)
            cell = f"{local}/{rel.with_suffix('').as_posix()}"
            block = "code block" if s.kind in ("code", "header") else "instance"
            if s.kind == "subckt":                 # the .subckt line: the cell's name and its symbol's pin names
                what = f".subckt line of {s.cell}" if f == s.home else f"its pins, as the .subckt line of {s.cell},"
            else:
                what = f"{block} {s.name}" if f == s.home else f"its symbol, as {block} {s.name} in {s.cell},"
            g = groups.setdefault(d, _group(d, local, key))
            g["items"] += [(f"{local}/{rel.as_posix()}", cell, what, x) for x in found]
    _refuse(groups)


_TCL_SEEN: dict[tuple[str, str], list[tuple[str, Finding]]] = {}
_TCL_LOCK = threading.Lock()


def _tcl_scan(d: Path) -> list[tuple[str, Finding]]:
    """(path below d, finding) for every .sch and .sym at any depth that Xschem would evaluate Tcl in. Kept per
    directory and installed tree (the marker's tree or sha): an install changes only by being replaced."""
    tag = next((v for v in (_marker(d).get(k) for k in ("tree", "sha")) if isinstance(v, str) and v), None)
    key = (str(d.resolve()), tag) if tag else None
    with _TCL_LOCK:
        if key in _TCL_SEEN:
            return _TCL_SEEN[key]
    out = []
    for p in sorted(x for x in d.rglob("*") if x.suffix in (".sch", ".sym") and x.is_file()):
        rel = p.relative_to(d).as_posix()
        try:
            out += [(rel, replace(f, where=rel)) for f in tcl_findings(_read(p))]
        except OSError as e:                       # what cannot be read cannot be passed
            out.append((rel, Finding("unparsed", 0, _short(f"cannot read {rel}: {e}"), where=rel)))
    if key:
        with _TCL_LOCK:
            _TCL_SEEN[key] = out
    return out


def check_tcl(untrusted: dict[Path, tuple[str, str]]) -> None:
    """Refuses (UntrustedContent) when any untrusted dependency has a .sch or .sym with Tcl that Xschem would
    evaluate: Xschem's own netlister reads whatever symbols it finds, so every untrusted install is checked, whether
    or not the cell being netlisted places it."""
    groups: dict[Path, dict] = {}
    for d, (local, key) in untrusted.items():
        if found := _tcl_scan(Path(d)):
            g = groups.setdefault(Path(d), _group(Path(d), local, key))
            g["items"] += [(f"{local}/{rel}", f"{local}/{rel}", "", f) for rel, f in found]
    _refuse(groups)


def _dep_of(lib_dir: Path) -> tuple[Path, Path] | None:
    """(project directory, install directory) when `lib_dir` is a dependency's install (<project>/.undine/deps/<local>,
    as given or once links are resolved), else None."""
    from undine.store import deps
    for p in (Path(lib_dir).absolute(), Path(lib_dir).resolve()):
        if p.parent.name == "deps" and p.parent.parent.name == ".undine":
            project = p.parent.parent.parent
            return project, deps._deps_root(project) / p.name      # refuses a .undine or .undine/deps that is a link
    return None


def check_rtl(lib_dir: Path, *, full: bool, texts: dict[str, str] | None = None) -> None:
    """Refuses (UntrustedContent) Verilator (or LibreLane's Verilator lint and Yosys) on a dependency library's RTL
    that rtl_findings reports (full=False, for lint: `include paths only), unless the project trusts that dependency.
    Every RTL file at any depth is read, as an `include can name one in a folder; `texts` ({file name: text}) is RTL
    made from the library's files (a schematic's gate-level export), read as the library's own. A project library is
    not checked."""
    dep = _dep_of(lib_dir)
    if dep is None:
        return
    project, d = dep
    key = key_of(d)
    if key in trusted_keys(project):
        return
    g = _group(d, d.name, key)
    files = [(p.relative_to(d).as_posix(), _read(p))
             for p in sorted(x for x in d.rglob("*") if x.suffix in RTL_SUFFIXES and x.is_file())]
    for rel, text in [*files, *(texts or {}).items()]:
        g["items"] += [(f"{d.name}/{rel}", f"{d.name}/{rel}", "", replace(f, where=rel))
                       for f in rtl_findings(text, full=full)]
    _refuse({d: g})


def set_trusted(prj, key: str, on: bool) -> bool:
    """Trusts `key` in project `prj` (on) or takes that back; whether project.json changed. A person's decision: the
    CLI asks, the browser's dialog takes a confirmation, and no agent tool reaches this. Refused in a changeset's
    preview: trust is the project's, and reaches its changesets from there (trusted_keys)."""
    if not isinstance(key, str) or not KEY.match(key):
        raise ValueError(f"{key!r} is not a trust key: <package>@<version> (analog_ip@1.2.0), or <name>@<commit12> "
                         "for a dependency without a manifest")
    if "~" in str(getattr(prj, "name", "")) or _real_project(prj.path) is not None:
        raise ValueError("trust is given in the project, not in a changeset preview: it reaches the changeset from "
                         "there")
    with prj.editing():
        t = prj.meta.get("trusted", [])
        if not isinstance(t, list):
            raise ValueError("project.json: trusted is not a list of keys: fix it by hand (a list such as "
                             "[\"analog_ip@1.2.0\"]), then try again")
        if (key in t) == on:
            return False
        t = [*t, key] if on else [k for k in t if k != key]
        if t:
            prj.meta["trusted"] = t
        else:
            prj.meta.pop("trusted", None)
        prj.save()
    return True


def drop(meta: dict, packages_or_locals) -> list[str]:
    """Takes the trust entries of `packages_or_locals` (package names, or local names for keys of dependencies
    without a manifest) out of project.json's `meta`, in place, with no I/O: a new commit or tree is other content
    and is trusted again only by a person. The entries taken, in their order."""
    names = set(packages_or_locals)
    t = meta.get("trusted")
    if not isinstance(t, list):
        return []
    gone = [k for k in t if isinstance(k, str) and k.partition("@")[0] in names]
    if gone:
        kept = [k for k in t if k not in gone]
        if kept:
            meta["trusted"] = kept
        else:
            meta.pop("trusted", None)
    return gone
