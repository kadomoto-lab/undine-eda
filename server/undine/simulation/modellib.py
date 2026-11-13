"""Fast loading of the PDK's ngspice model library: a copy of a library section reduced to what a netlist can reach.

A netlist reads its models with `.lib <PDK>/libs.tech/ngspice/sky130.lib.spice tt` (the line Undine writes, or the PDK's
corner symbol), and ngspice reads that whole section: for SKY130 170 files, 7 MB, 188 subcircuits with a thousand
models in them and 377 .param cards with some 12 000 parameters, about 20 s before a one-transistor .op starts (a
minute or two each when ten jobs run at once). Almost all of it belongs to devices the netlist does not use. `reduce`
points such a line at a copy of the section that holds what the netlist can reach of it (0.2 s for that .op), made
once per (PDK version, library, section, the definitions kept) and kept in cache_dir(). The copy holds, in the
library's order (its .include files and nested .lib sections inlined), each card as the library writes it:

* every .option and .func card (`.option scale=1.0u` applies to the whole netlist), and every card outside a subcircuit
  that can draw random numbers (agauss, gauss, aunif, unif, limit, or a function defined with one): whether ngspice
  evaluates one that nothing uses or not, a Monte Carlo run then draws the same numbers from the same seed (a subcircuit
  draws only when the netlist instantiates it, and then it is kept)
* the .subckt, .model and .param definitions of every name the netlist mentions (it and the files it includes: a
  standard-cell library's SPICE, a code block's .include), and of every name those mention, transitively. A model
  `name.N` is a bin of `name`; a .param card goes when any name it assigns is needed. A .param assignment that nothing
  kept mentions is unreachable: no expression can read it, and evaluating it draws nothing

so ngspice evaluates the same definitions in the same order and the results are the same to the last bit
(tests/test_modellib.py compares them per device family, corner, mismatch section and temperature). A netlist that
reads several sections of the library (GF180's test benches: typical, cap_mim, res_typical, mimcap_typical ...) has
them reduced together: one section's subcircuit may read a parameter only another defines, or one another redefines,
so each keeps the definitions of what any card kept in any of them mentions; and when one is read whole, all are.

Anything it does not understand falls back to the library itself for that job, and the job says why (its status'
"models", the first lines of its log): a card other than those (.if, .end, an element line outside a
subcircuit), an .include inside a subcircuit, a file it cannot read, a .param whose assignments it cannot list, a
section the library does not have, a line of the netlist (or a file it includes) that reads a file it cannot tell,
or reads the library other than as `.lib <library> <section>`, a .control block that sources a file; and an error of
its own. UNDINE_SIM_FULL_MODELS=1 turns it off.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from undine import tools

VERSION = 1                                    # of the reduction: a copy another version made is not used
FULL_ENV = "UNDINE_SIM_FULL_MODELS"
KEEP_DAYS = 30                                 # a copy unused for this long is removed when another one is made

_TOK = re.compile(r"[a-z_][a-z0-9_]*")         # a name as cards mention it (lower case: SPICE ignores case)
# what names_in reads: letters (in lower case), digits and _; anything else separates
_SEP = str.maketrans({chr(c): (chr(c).lower() if chr(c).isascii() and (chr(c).isalnum() or chr(c) == "_") else " ")
                      for c in range(256)})
# ngspice's functions that draw a random number when an expression is evaluated
RANDOM = frozenset({"agauss", "gauss", "aunif", "unif", "limit", "sgauss", "sunif", "rand", "rnd", "random",
                    "poisson", "exponential"})
# `.lib <file> <section>` in a netlist (as api._set_corner reads it)
_LIB_LINE = re.compile(r"^([ \t]*\.lib[ \t]+)(['\"]?)(\S+?)\2([ \t]+)(\S+)[ \t]*$", re.M | re.I)
# a line that reads a file, as ngspice tells one: its first word starts with .inc (.include) or .lib; then the rest
_READS = re.compile(r"^[ \t]*(\.(?:inc|lib)[^\s'\"]*)([^\n]*)", re.M | re.I)
_WORD = re.compile(r"\s*(\"[^\"]*\"|'[^']*'|[^\s'\"]+)")   # a word of it: quoted (blanks and all) or not
_CONTROL = re.compile(r"^[ \t]*\.control\b.*?^[ \t]*\.endc\b", re.M | re.S | re.I)
_ASSIGN = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*)\s*(\([^()=]*\))?\s*=(?!=)")
_EQ = re.compile(r"(?<![=!<>])=(?!=)")         # an assignment's =, not ==, !=, <=, >=


class Unsupported(Exception):
    """Something in the library or the netlist the reducer does not understand: the job reads the library whole."""
    section: str | None = None                 # the section it is in, of those the netlist reads (reduce says which)


@dataclass(frozen=True)
class Card:
    kind: str                                  # subckt | model | param | func | option
    names: frozenset                           # what it defines (a model's bins under their base name)
    refs: frozenset                            # every name it mentions
    start: int                                 # its text in its file (characters of the file read as latin-1)
    end: int
    always: bool = False                       # kept whatever the netlist uses (.option, .func, a name not like _TOK)
    funcs: frozenset = frozenset()             # the functions it defines (.func, .param f(x) = ...)


def cache_dir() -> Path:
    """Where the reduced copies are: $UNDINE_CACHE/models, else $XDG_CACHE_HOME/undine/models (by default
    ~/.cache/undine/models). Absolute, as a netlist names a copy and ngspice runs in the job's directory: a relative
    $UNDINE_CACHE is taken from the server's working directory, a relative $XDG_CACHE_HOME is ignored (as the XDG
    spec says)."""
    return tools.cache_root() / "models"


def full_models() -> bool:
    return os.environ.get(FULL_ENV, "").strip().lower() not in ("", "0", "false", "no")


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("latin-1")      # every byte stays what it is: cards are copied as written
    except OSError as e:
        raise Unsupported(f"cannot read {path} ({e.strerror or e})") from None


def names_in(text: str) -> set[str]:
    """The names `text` mentions: its runs of letters, digits and _ that do not start with a digit, in lower case."""
    return {t for t in set(text.translate(_SEP).split()) if not t[0].isdigit()}


def _stat(path: Path) -> tuple[int, int]:
    """(size, mtime) of a file; (-1, -1) when there is none."""
    try:
        st = path.stat()
    except OSError:
        return -1, -1
    return st.st_size, st.st_mtime_ns


def _code(line: str) -> str:
    """A line without its trailing comment (ngspice: ; and // anywhere, $ after a blank)."""
    return re.split(r";|//|(?<=\s)\$", line, maxsplit=1)[0]


def _includes(text: str, where: str) -> list[str]:
    """The files `text` reads, as written: `.include <file>`, `.lib <file> <section>` (a path quoted, with blanks in
    it, or not; a trailing comment; a CR line end), not `.lib <section>` (a section's start). Unsupported for a line
    that reads a file it cannot tell: the names in that file would be missed."""
    files = []
    for m in _READS.finditer(text):
        rest, words, pos = _code(m.group(2)).strip(), [], 0
        while pos < len(rest) and (w := _WORD.match(rest, pos)):
            words.append(w.group(1)[1:-1] if w.group(1)[0] in "'\"" else w.group(1))
            pos = w.end()
        n, lib = len(words), m.group(1).lower().startswith(".lib")
        if pos < len(rest) or n not in ((1, 2) if lib else (1,)):
            raise Unsupported(f"cannot tell the file {m.group(0).strip()[:60]!r} reads ({where})")
        if n == (2 if lib else 1):             # not a .lib <section>
            files.append(words[0])
    return files


def _param_names(lines: list[str]) -> tuple[set[str], set[str]]:
    """The names a .param card assigns, and which of them are functions (`.param f(x) = 2*x`). Unsupported when it
    cannot tell them all: every = outside quotes and braces must be one of a `name =`."""
    body = " ".join(_code(ln).strip().lstrip("+") for ln in lines).lower()
    body = re.sub(r"^\s*\.param\b", " ", body)
    body = re.sub(r"'[^']*'", " ", body)
    while (b := re.sub(r"\{[^{}]*\}", " ", body)) != body:
        body = b
    if re.search(r"['{}]", body) or len(_ASSIGN.findall(body)) != len(_EQ.findall(body)):
        raise Unsupported(f"cannot list the assignments of {lines[0].strip()[:60]}")
    names, funcs = set(), set()
    for name, args in _ASSIGN.findall(body):
        names.add(name)
        if args:
            funcs.add(name)
    return names, funcs


_HEAD = re.compile(r"^[ \t]*([^\s+*][^\n]*)", re.M)      # a line that starts a card: not blank, * or +
_COMMENT_LINE = re.compile(r"^[ \t]*\*[^\n]*", re.M)
_CONTINUED = re.compile(r"^[ \t]*\+", re.M)
CARDS = {".subckt": "subckt", ".model": "model", ".param": "param", ".func": "func", ".option": "option",
         ".options": "option"}


def _card(kind: str, text: str, start: int, end: int) -> Card:
    """The card text[start:end] (its comment lines in it stay: ngspice reads past them)."""
    code = _COMMENT_LINE.sub("", text[start:end])
    refs = frozenset(names_in(code))
    head = code.split(None, 2)
    if kind == "param":
        names, funcs = _param_names([ln for ln in code.split("\n") if ln.strip()])
        return Card(kind, frozenset(names), refs, start, end, funcs=frozenset(funcs))
    if kind == "func":
        m = re.match(r"\s*\.func\s+([^\s(]+)", code, re.I)
        name = m.group(1).lower() if m else ""
        return Card(kind, frozenset({name}), refs, start, end, always=True, funcs=frozenset({name}))
    if kind == "option":
        return Card(kind, frozenset(), refs, start, end, always=True)
    if len(head) < 2:
        raise Unsupported(f"a {head[0]} card without a name")
    name = head[1].lower()
    if kind == "model":
        name = re.sub(r"\.\d+$", "", name)         # binned: name.0, name.1 ... are found by name
    # a name the netlist could write other than as _TOK finds it: kept, rather than missed
    return Card(kind, frozenset({name}), refs, start, end, always=not _TOK.fullmatch(name))


def _trim(text: str, start: int, end: int) -> int:
    """`end` moved back over the blank and comment lines before it (to the end of the card's last line)."""
    while True:
        while end > start and text[end - 1] in " \t\r\n":
            end -= 1
        line = max(text.rfind("\n", start, end) + 1, start)
        if line == start or not text[line:end].lstrip().startswith("*"):
            return end
        end = line


@functools.lru_cache(maxsize=1024)
def _parse(path: str, section: str | None, stamp: tuple[int, int]) -> tuple[tuple, str | None]:
    """The cards of a file (or of one of its .lib sections) and the files it reads, in order: ("card", Card) and
    ("unit", path, section); or why it cannot (Unsupported's message). `stamp` (size, mtime) is the file's, so a
    changed file is read again."""
    try:
        return _parse_text(_read(Path(path)), path, section), None
    except Unsupported as e:
        return (), str(e)


def _parse_text(text: str, path: str, section: str | None) -> tuple:
    base = Path(path).parent
    heads = list(_HEAD.finditer(text))
    i = 0
    if section is not None:                    # from `.lib <section>` to `.endl`
        i = next((k + 1 for k, h in enumerate(heads) if h.group(1).lower().split() == [".lib", section.lower()]), 0)
        if not i:
            raise Unsupported(f"no section {section} in {path}")
    if _CONTINUED.search(text, heads[i - 1].end() if i else 0, heads[i].start() if i < len(heads) else len(text)):
        raise Unsupported(f"a continuation line (+) without a card before it ({path})")
    items: list = []
    while i < len(heads):
        h = heads[i]
        t = h.group(1).split()
        word = t[0].lower()
        j = i + 1
        if word == ".subckt":                  # to its .ends, with the subcircuits in it
            depth = 1
            while j < len(heads) and depth:
                w = heads[j].group(1).split()[0].lower()
                depth += (w == ".subckt") - (w == ".ends")
                if w in (".include", ".inc", ".lib", ".endl"):
                    raise Unsupported(f"{w} inside a subcircuit ({path})")
                j += 1
            if depth:
                raise Unsupported(f"subcircuit {t[1] if len(t) > 1 else ''} has no .ends ({path})")
        stop = heads[j].start() if j < len(heads) else len(text)
        if word == ".endl" and section is not None:
            return tuple(items)
        if word in CARDS:
            items.append(("card", _card(CARDS[word], text, h.start(), _trim(text, h.start(), stop))))
        elif _CONTINUED.search(text, h.end(), stop):
            raise Unsupported(f"a {t[0]} card continued on the next line ({path})")
        elif word in (".include", ".inc") and len(t) == 2:
            items.append(("unit", str((base / t[1].strip("'\"")).resolve()), None))
        elif word == ".lib" and len(t) == 3:
            items.append(("unit", str((base / t[1].strip("'\"")).resolve()), t[2]))
        elif word.startswith("."):
            raise Unsupported(f"a {t[0]} card ({path})")
        else:
            raise Unsupported(f"an element line outside a subcircuit ({path}: {h.group(1)[:40]})")
        i = j
    if section is not None:
        raise Unsupported(f"section {section} of {path} has no .endl")
    return tuple(items)


@dataclass
class Section:
    """A library section with its files inlined: its cards in order, the files they are in, and their stamps."""
    lib: str
    name: str
    cards: list                                # (file index, Card)
    files: list                                # paths
    stamps: list                               # (path, size, mtime) of every file read
    by_name: dict                              # name -> indexes of the cards that define it
    kept: frozenset                            # indexes of the cards kept whatever the netlist uses


def _flatten(lib: Path, name: str, stamps: dict) -> Section:
    """The section, its files inlined; `stamps` gets the (size, mtime) of each file read (also when it fails)."""
    files: list[str] = []
    cards: list = []

    def walk(path: str, sec: str | None, stack: tuple):
        if (path, sec) in stack:
            raise Unsupported(f"{path} includes itself")
        stamps[path] = stamps.get(path) or _stat(Path(path))
        if stamps[path][0] < 0:
            raise Unsupported(f"cannot read {path}")
        if path not in files:
            files.append(path)
        fi = files.index(path)
        items, why = _parse(path, sec, stamps[path])
        if why:
            raise Unsupported(why)
        for item in items:
            if item[0] == "card":
                cards.append((fi, item[1]))
            else:
                walk(item[1], item[2], stack + ((path, sec),))
    walk(str(lib), name, ())
    by_name: dict[str, list[int]] = {}
    for i, (_, c) in enumerate(cards):
        for n in c.names:
            by_name.setdefault(n, []).append(i)
    # what draws random numbers when the netlist is read: the random functions, and functions defined with one
    rnd, more = set(RANDOM), True
    while more:
        more = False
        for _, c in cards:
            if c.funcs and c.refs & rnd and not c.funcs <= rnd:
                rnd |= c.funcs
                more = True
    kept = frozenset(i for i, (_, c) in enumerate(cards) if c.always or (c.kind != "subckt" and c.refs & rnd))
    return Section(str(lib), name, cards, files, [(p, *stamps[p]) for p in files], by_name, kept)


_SECTIONS: "OrderedDict[tuple[str, str], tuple[dict, Section | str]]" = OrderedDict()
_LOCK = threading.Lock()
_BUILDING: dict[str, threading.Lock] = {}


def section(lib: Path, name: str) -> Section:
    """The section, read once while its files stay as they are (the last 16 sections read, or why they cannot be);
    Unsupported when it cannot be reduced."""
    key = (str(lib), name)
    with _LOCK:
        hit = _SECTIONS.get(key)
    if hit is not None and all(_stat(Path(p)) == st for p, st in hit[0].items()):
        with _LOCK:
            _SECTIONS.move_to_end(key)
        sec = hit[1]
    else:
        stamps: dict = {}
        try:
            sec = _flatten(lib, name, stamps)
        except Unsupported as e:
            sec = str(e)
        with _LOCK:
            _SECTIONS[key] = (stamps, sec)
            while len(_SECTIONS) > 16:
                _SECTIONS.popitem(last=False)
    if isinstance(sec, str):
        raise Unsupported(sec)
    return sec


def closure(secs: list[Section], names: set[str]) -> list[list[int]]:
    """Indexes of the cards to keep of each section, for a netlist that mentions `names` and reads them all: those
    kept anyway, the definitions of the names, and of every name a kept card (of any of them) mentions."""
    keep = [set(s.kept) for s in secs]
    work = [n for s, k in zip(secs, keep) for i in k for n in s.cards[i][1].refs] + list(names)
    seen: set[str] = set()
    while work:
        n = work.pop()
        if n in seen:
            continue
        seen.add(n)
        for s, k in zip(secs, keep):
            for i in s.by_name.get(n, ()):
                if i not in k:
                    k.add(i)
                    work.extend(s.cards[i][1].refs)
    return [sorted(k) for k in keep]


def _paths(base: Path, files: list[str]) -> list[str]:
    return [str((base / os.path.expanduser(f)).resolve()) for f in files]


@functools.lru_cache(maxsize=64)
def _file_tokens(path: str, stamp: tuple[int, int]) -> tuple[frozenset, tuple]:
    """The names a file the netlist includes mentions, and the files it includes in turn."""
    text = _read(Path(path))
    return frozenset(names_in(text)), tuple(_paths(Path(path).parent, _includes(text, path)))


def netlist_names(text: str, cwd: Path | None, lib: Path | None = None) -> set[str]:
    """Every name the netlist mentions, with those of the files it includes (whole: any section of them, and the
    files they include), relative paths from `cwd`. Unsupported when one cannot be read, a line reads a file it cannot
    tell (_includes), one reads the library `lib` itself (a line reduce does not point at a copy: the section it reads
    whole may need any definition of the others), or a .control block sources a file."""
    for block in _CONTROL.findall(text):
        if re.search(r"^[ \t]*source\b", block, re.M | re.I):
            raise Unsupported("a .control block of the netlist sources a file, whose devices are unknown here")
    names = names_in(text)
    todo = _paths(cwd or Path.cwd(), _includes(text, "the netlist"))
    seen: set[str] = set()
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        if lib is not None and p == str(lib):
            raise Unsupported(f"the netlist reads {lib.name} other than in a line `.lib {lib.name} <section>`")
        stamp = _stat(Path(p))
        if stamp[0] < 0:
            raise Unsupported(f"cannot read {p}, which the netlist includes")
        toks, inc = _file_tokens(p, stamp)
        names |= toks
        todo.extend(inc)
    return names


def _own(text: str) -> bytes:
    """A line of the copy's own (its header, .lib and .endl): as the netlist naming it is written, paths as the file
    system has them."""
    return text.encode("utf-8", "surrogateescape")


def _write(sec: Section, keep: list[int], path: Path, header: str) -> None:
    texts = [Path(f).read_bytes() for f in sec.files]
    out = [_own(header), _own(f".lib {sec.name}")]
    out += [texts[fi][c.start:c.end] for fi, c in (sec.cards[i] for i in keep)]   # (read as latin-1: bytes)
    out.append(_own(f".endl {sec.name}\n"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(b"\n".join(out))
            f.flush()
            os.fsync(f.fileno())               # on disk before it has its name: a crash leaves no copy cut short
        os.replace(tmp, path)                  # whole or not at all, for jobs of other processes reading it
    finally:
        tmp.unlink(missing_ok=True)
    now = time.time()
    for f in path.parent.iterdir():            # copies no job has used for KEEP_DAYS, and what a crash left
        try:
            age = now - f.stat().st_mtime
            if (f.suffix == ".spice" and age > KEEP_DAYS * 86400) or (f.suffix == ".tmp" and age > 86400):
                f.unlink()
        except OSError:
            pass


def _whole(path: Path, name: str) -> bool:
    """Whether the copy at `path` ends as _write ends one (.endl is its last line, and in no card of it): one cut
    short (a crash before it was on disk, an edit by hand) is made again, not read by every job after it."""
    tail = _own(f"\n.endl {name}\n")
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - len(tail)))
            return f.read() == tail
    except OSError:
        return False


def reduced_copy(sec: Section, keep: list[int], roots: list[str], pdk: str = "") -> Path:
    """The copy of section `sec` with the cards `keep` (closure), made when there is none yet or the one there is not
    whole; `roots`: the names of the netlist it defines (for the copy's header)."""
    key = hashlib.sha256(json.dumps([VERSION, pdk, sec.lib, sec.name, sec.stamps, keep]).encode()).hexdigest()[:20]
    tag = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{pdk or 'pdk'}-{Path(sec.lib).name}-{sec.name}")
    path = cache_dir() / f"{tag}-{key}.spice"
    with _LOCK:
        lock = _BUILDING.setdefault(str(path), threading.Lock())
    with lock:                                 # one thread makes it; the others of this process wait and use it
        made = _stat(path)
        if made[0] < 0 or not _whole(path, sec.name):
            uses = f" that uses {', '.join(roots[:12])}{' ...' if len(roots) > 12 else ''}" if roots else ""
            header = (f"* Undine: {len(keep)} of the {len(sec.cards)} cards of section {sec.name} of {sec.lib}"
                      f"{f' ({pdk})' if pdk else ''}, what a netlist{uses} can reach of it"
                      " (undine/simulation/modellib.py)")
            try:
                _write(sec, keep, path, header)
            except OSError as e:
                raise Unsupported(f"cannot make the reduced copy {path} ({e.strerror or e})") from None
        elif time.time() - made[1] / 1e9 > 86400:
            try:
                os.utime(path)                 # in use: not removed as unused
            except OSError:
                pass
    return path


def _copies(text: str, lines: list, lib: Path, pdk: str, cwd: Path | None) -> dict[str, tuple[Path, int, int]]:
    """Per section the netlist's `lines` read: its reduced copy, the cards it keeps and the section's cards. The
    sections are reduced together (closure); Unsupported when any one cannot be (its `section` says which)."""
    names = netlist_names(_without(text, lines), cwd, lib)
    secs: dict[str, Section] = {}
    for m in lines:
        if m.group(5) not in secs:
            try:
                secs[m.group(5)] = section(lib, m.group(5))
            except Unsupported as e:
                e.section = m.group(5)
                raise
    keeps = closure(list(secs.values()), names)
    return {s: (reduced_copy(sec, keep, sorted(names & sec.by_name.keys()), pdk), len(keep), len(sec.cards))
            for (s, sec), keep in zip(secs.items(), keeps)}


def reduce(text: str, lib: Path, pdk: str = "", cwd: Path | None = None) -> tuple[str, list[dict]]:
    """`text` with each `.lib <lib> <section>` line pointing at the section's reduced copy, a comment above it naming
    the library and section; and per line what was used: {library, section, reduced: path, cards: [kept, of]}, or
    {library, section, full: why} when the lines stay (a library or netlist it does not understand, an error of its
    own, UNDINE_SIM_FULL_MODELS: all of them, as one section may need what another defines). `pdk` (its id, with the
    version) is part of the copy's name and key; `cwd`: where the netlist's relative .include paths start (the job's
    directory)."""
    try:
        lib = lib.resolve()
    except (OSError, RuntimeError):            # (a symlink loop)
        return text, []
    lines = []
    for m in _LIB_LINE.finditer(text):
        try:
            if ((cwd or Path.cwd()) / os.path.expanduser(m.group(3))).resolve() == lib:
                lines.append(m)
        except (OSError, RuntimeError):
            pass
    if not lines:
        return text, []
    records = [{"library": str(lib), "section": m.group(5)} for m in lines]
    if full_models():
        return text, [{**r, "full": f"{FULL_ENV} is set"} for r in records]
    try:
        copies = _copies(text, lines, lib, pdk, cwd)
    except Unsupported as e:
        return text, [{**r, "full": str(e) if e.section in (None, r["section"]) else
                       f"with section {e.section}, which is read whole: {e}"} for r in records]
    except Exception as e:                     # the reducer's own error: the job runs all the same, on the library
        return text, [{**r, "full": f"the reducer failed ({type(e).__name__}: {e})"} for r in records]
    out, pos = [], 0
    for m, r in zip(lines, records):
        out.append(text[pos:m.start()])
        pos = m.end()
        path, kept, total = copies[m.group(5)]
        r.update(reduced=str(path), cards=[kept, total])
        q = '"' if re.search(r"\s", str(path)) else ""
        out.append(f"* Undine: the models of {lib} {m.group(5)}, from a copy reduced to what this netlist uses "
                   f"({FULL_ENV}=1 reads the library itself)\n{m.group(1)}{q}{path}{q}{m.group(4)}{m.group(5)}")
    out.append(text[pos:])
    return "".join(out), records


def _without(text: str, lines: list) -> str:
    """`text` without the given `.lib` lines (their path and section are not names the netlist uses)."""
    out, pos = [], 0
    for m in lines:
        out.append(text[pos:m.start()])
        pos = m.end()
    return "".join(out) + text[pos:]


def describe(record: dict) -> str:
    """One line on how a job read a model library section, for its log."""
    where = f"{Path(record['library']).name} section {record['section']}"
    if "full" in record:
        return f"models: {where} read whole ({record['full']})"
    kept, total = record.get("cards", ["?", "?"])
    return f"models: {where} as a copy reduced to the {kept} of its {total} cards this netlist can reach " \
           f"({record['reduced']})"
