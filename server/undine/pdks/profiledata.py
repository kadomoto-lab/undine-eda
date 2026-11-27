"""PDK profiles as data: profile.toml, read and checked (docs/design/packages.md, PDK profiles).

A profile package holds what Undine needs to know about the processes of one ciel family (`profile_sky130`,
`profile_gf180mcu`, `profile_ihp_sg13`: SG13G2 and SG13CMOS5L): a directory with an undine-package.toml (the package's
name and version, as any package) and a profile.toml:

    schema = 1                    # the profile format; a newer one is refused
    undine = ">=0.1.0"            # the Undine versions that read it
    family = "sky130"             # the ciel family

    [[profile]]                   # one per process; a pdkprofile.Profile
    name = "sky130"               # Profile.family, the key for_family and the layout code use
    label = "SkyWater SKY130"
    variants = ["sky130A", "sky130B"]   # the PDK variants it describes, each in one [[profile]] only
    layers = { ... }              # every other key is the Profile field of that name
    [profile.stack]               # a Stack written out, or one a named generator makes (formulas.STACKS):
    generator = "gf180_stack@1"   #   its arguments beside it
    thin = 4

    [[pdk]]                       # the PDK commits the profile knows
    commit = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"
    tested = true                 # Undine's tests run on it: the default to install (tested_commits)
    license = "Apache-2.0"
    compat = ["sky130/2025"]      # the compatibility tags it is in
    [pdk.tree.sky130A]            # its expected tree hashes, per variant and part
    "libs.tech" = "sha256:..."

A key is converted by the field's annotation (a list becomes a tuple, a table a dataclass or a dict, an integer in a
float field a float); an untyped dict (drc_options, lvs_options, rename, implant) keeps the TOML types, since a deck
gets `str(value)`. Unknown keys are refused, not ignored: an Undine that dropped a field would run DRC or LVS
differently. `verified` and `magic_drc` are not profile keys: they record what Undine's own tests have run and come
from Undine's table (pdks/tested.py). Computed values are named, never written as code: `params =
"mim_area_perimeter"` and the stack generators are functions of formulas.py. Refused besides: a template placeholder a
field does not fill, a regex that does not compile, a path that is absolute or has a `..` part, a PCell library that
names a module, a function or an environment Undine would not run.

Undine ships its current profiles built in (undine/data/profiles/<package>/), the same files a profile package
publishes, as the fallback when no index has one. Pure: no network, no writes. Every error reads
`<where>: profile.toml: <key path>: <what>: <fix>`, as manifest.py's do.
"""
from __future__ import annotations

import dataclasses
import functools
import inspect
import math
import re
import string
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:                  # Python 3.10: the same parser, packaged as tomli
    import tomli as tomllib

from undine.pdks import formulas
from undine.pdks.pdkprofile import PcellLib, Profile, Stack

SCHEMA = 1
PROFILE_TOML = "profile.toml"
BUILTIN = Path(__file__).resolve().parents[1] / "data" / "profiles"
BUILTIN_PACKAGES = ("profile_sky130", "profile_gf180mcu", "profile_ihp_sg13")
PREFIX = "profile_"                          # the package names reserved for profile packages
PCELL_ENVS = ("pcell", "pcell-gf180")        # the PCell environments pixi.toml defines (pcell.python_exe)
SWITCH_MODES = ("flags", "tags", "negated")  # Profile.drc_switches, as Profile.drc_rd spells them

_TOP = ("schema", "undine", "family", "profile", "pdk")
_PROFILE_ONLY = ("name", "variants")
_UNDINE_SIDE = ("verified", "magic_drc")     # Undine's table (tested.py), never the profile's
_PDK_KEYS = ("commit", "compat", "tested", "license", "tree")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# what a path or name of a profile never holds: they reach a netlist, an xschemrc (Tcl) or a shell-free command line
# as written, so no whitespace and nothing Tcl or SPICE reads as syntax (braces only as a template's placeholders)
_UNSAFE = re.compile(r"[\s\[\]$\"'`;]")
_WORD = re.compile(r"^[A-Za-z0-9_.+-]+$")            # a corner, a section, a stdcell library name
_GROUP = re.compile(r"\\(\d+)|\\g<(\w+)>")

# the placeholders a template field is formatted with (Profile.magic_file, stdcell, ...); none: not a template
_TEMPLATES = {"magic_rc": {"name"}, "netgen_setup": {"name"}, "mismatch_section": {"corner"},
              "stdcell_gds": {"lib"}, "stdcell_spice": {"lib", "cell"}, "stdcell_verilog": {"lib"}}
# fields holding a path relative to the PDK root, libs.tech/<tool> or the decks directory
_PATHS = ("klayout_dir", "drc_klayout", "drc_extra", "lvs_klayout", "magic_rc", "netgen_setup", "model_lib",
          "model_includes", "corner_symbol", "stdcell_gds", "stdcell_spice", "stdcell_verilog")


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class PdkEntry:
    """A PDK commit a profile knows: its compatibility tags, whether Undine's tests run on it, its licence, and its
    expected tree hashes ({variant: {part: "sha256:<hex>"}}; none yet: listed, not verifiable)."""
    commit: str
    compat: tuple[str, ...] = ()
    tested: bool = False
    license: str | None = None
    tree: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProfileSet:
    """One profile package, loaded: {name: Profile} in file order, {variant: profile name}, its [[pdk]] entries."""
    package: str
    version: str
    family: str
    undine: str
    profiles: dict
    variants: dict
    pdk: tuple = ()

    def tested_commits(self) -> tuple[str, ...]:
        """The commits Undine's tests run on (tested = true), in file order: the first is the one to install."""
        return tuple(e.commit for e in self.pdk if e.tested)


# ------------------------------------------------------------------ errors and paths


def _kp(parts) -> str:
    """A key path: profile[0].stack.cuts[2].name, profile[0].layers."li1.label"."""
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            s = p if re.match(r"^[A-Za-z0-9_-]+$", p) else '"' + p.replace('"', '\\"') + '"'
            out += ("." if out else "") + s
    return out


class _Reader:
    def __init__(self, where: str):
        self.where = where

    def err(self, path, what: str, fix: str = "") -> ProfileError:
        return ProfileError(": ".join(x for x in (self.where, PROFILE_TOML, _kp(path), what, fix) if x))

    # ------------------------------------------------------------- conversion by annotation

    def value(self, v, hint, path: list):
        origin, args = typing.get_origin(hint), typing.get_args(hint)
        if origin in (typing.Union, types.UnionType):            # X | None: absent is None, TOML has no null
            inner = [a for a in args if a is not type(None)]
            return self.value(v, inner[0], path)
        if hint is Stack:
            return self.stack(v, path)
        if dataclasses.is_dataclass(hint):
            return self.dataclass(hint, v, path)
        if origin is tuple:
            if not isinstance(v, list):
                raise self.err(path, f"{v!r} is not a list", "write it as [..]")
            if len(args) == 2 and args[1] is Ellipsis:
                return tuple(self.value(x, args[0], path + [i]) for i, x in enumerate(v))
            if len(v) != len(args):
                raise self.err(path, f"{len(v)} items, not {len(args)}", f"write {len(args)} items")
            return tuple(self.value(x, a, path + [i]) for i, (x, a) in enumerate(zip(v, args)))
        if origin is dict:
            if not isinstance(v, dict):
                raise self.err(path, f"{v!r} is not a table", "write it as a table")
            return {k: self.value(x, args[1], path + [k]) for k, x in v.items()}
        if hint is dict:
            return self.raw(v, path)
        if origin is Callable:
            return self.formula(v, path)
        if hint is bool:
            if not isinstance(v, bool):
                raise self.err(path, f"{v!r} is not true or false")
            return v
        if hint is int:
            if not isinstance(v, int) or isinstance(v, bool):
                raise self.err(path, f"{v!r} is not an integer")
            return v
        if hint is float:
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise self.err(path, f"{v!r} is not a number")
            try:
                return float(v)
            except OverflowError:
                raise self.err(path, f"{v!r} is too large for a number") from None
        if hint is str:
            if not isinstance(v, str):
                raise self.err(path, f"{v!r} is not a string", "quote it")
            if _CONTROL.search(v):
                raise self.err(path, f"{v!r} holds a control character (a newline, a tab)", "write it on one line")
            return v
        raise self.err(path, f"no profile field has the type {hint!r}")      # a Profile field Undine cannot read

    def raw(self, v, path: list) -> dict:
        """An untyped table (deck options, renames): strings, booleans, numbers and tables, as written."""
        if not isinstance(v, dict):
            raise self.err(path, f"{v!r} is not a table", "write it as a table")
        out = {}
        for k, x in v.items():
            if isinstance(x, dict):
                out[k] = self.raw(x, path + [k])
            elif isinstance(x, str) and _CONTROL.search(x):
                raise self.err(path + [k], f"{x!r} holds a control character (a newline, a tab)",
                               "write it on one line")
            elif isinstance(x, (str, bool, int, float)):
                out[k] = x
            else:
                raise self.err(path + [k], f"{x!r} is not a string, a boolean, a number or a table")
        return out

    def dataclass(self, cls, v, path: list, given: dict | None = None, refused: tuple = ()):
        if not isinstance(v, dict):
            raise self.err(path, f"{v!r} is not a table", "write it as a table")
        given = given or {}
        fields = {f.name: f for f in dataclasses.fields(cls) if f.name not in given}
        for k in v:
            if k not in fields or k in refused:
                raise self.unknown(path + [k], [*_PROFILE_ONLY, *fields] if cls is Profile else list(fields))
        hints = _hints(cls)
        kwargs = dict(given)
        for name, f in fields.items():
            if name in v:
                kwargs[name] = self.value(v[name], hints[name], path + [name])
            elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                raise self.err(path + [name], "missing", f"add {name}")
        return cls(**kwargs)

    def unknown(self, path: list, known) -> ProfileError:
        if path[-1] in _UNDINE_SIDE:
            return self.err(path, "not a profile key",
                            "it records what Undine's own tests ran: Undine's table (pdks/tested.py) gives it; drop it")
        return self.err(path, "unknown key (made for a newer Undine?)",
                        f"the known keys are {', '.join(k for k in known if k not in _UNDINE_SIDE)}")

    def formula(self, v, path: list):
        if not isinstance(v, str) or v not in formulas.FORMULAS:
            raise self.err(path, f"{v!r} is not a formula Undine has",
                           f"name one of {', '.join(formulas.FORMULAS)}")
        return formulas.FORMULAS[v]

    def stack(self, v, path: list) -> Stack:
        if not isinstance(v, dict) or "generator" not in v:
            return self.dataclass(Stack, v, path)
        name = v["generator"]
        fn = formulas.STACKS.get(name) if isinstance(name, str) else None
        if fn is None:
            raise self.err(path + ["generator"], f"{name!r} is not a stack generator Undine has",
                           f"name one of {', '.join(formulas.STACKS)}, or write the stack out")
        params = inspect.signature(fn).parameters
        hints = typing.get_type_hints(fn)
        args = {}
        for k, x in v.items():
            if k == "generator":
                continue
            if k not in params:
                raise self.err(path + [k], f"not an argument of {name}", f"its arguments are {', '.join(params)}")
            args[k] = self.value(x, hints[k], path + [k])
        for k, p in params.items():
            if k not in args and p.default is inspect.Parameter.empty:
                raise self.err(path + [k], f"missing (an argument of {name})", f"add {k}")
        try:
            return fn(**args)
        except (ValueError, IndexError, TypeError) as e:
            raise self.err(path, f"{name} cannot make a stack of these arguments: {e}") from None

    # ------------------------------------------------------------- the file

    def document(self, doc: dict, package: str, version: str, undine_version: str | None) -> ProfileSet:
        from undine.pdks import tested
        from undine.pdks.pdk import family_of
        from undine.store.semver import Req, SemverError, Version

        for k in doc:
            if k not in _TOP:
                raise self.unknown([k], _TOP)
        schema = doc.get("schema")
        if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
            raise self.err(["schema"], "missing" if schema is None else f"{schema!r} is not a profile format",
                           f"write schema = {SCHEMA}")
        if schema > SCHEMA:
            raise self.err(["schema"], f"profile format {schema}, this Undine reads format {SCHEMA}",
                           "update Undine, or use a profile version made for this one")
        undine = doc.get("undine")
        try:
            req = Req.parse(undine)
        except SemverError as e:
            raise self.err(["undine"], "missing" if undine is None else str(e),
                           'write e.g. undine = ">=0.1.0"') from None
        try:
            mine = Version.parse(undine_version) if undine_version else None
        except SemverError:
            mine = None                                  # an Undine with no version to compare: not refused for it
        if mine is not None and not req.matches(mine):
            raise self.err(["undine"], f"made for Undine {undine}, this is {undine_version}",
                           "update Undine, or use a profile version made for this one")
        family = doc.get("family")
        if not isinstance(family, str) or not _NAME.match(family):
            raise self.err(["family"], "missing" if family is None else f"{family!r} is not a PDK family",
                           "write ciel's family name, e.g. sky130, gf180mcu or ihp-sg13")
        entries = doc.get("profile")
        if not isinstance(entries, list) or not entries or not all(isinstance(t, dict) for t in entries):
            raise self.err(["profile"], "missing" if entries is None else "not an array of tables",
                           "write each process as a [[profile]] table")
        profiles: dict[str, Profile] = {}
        variants: dict[str, str] = {}
        for i, t in enumerate(entries):
            name = t.get("name")
            if not isinstance(name, str) or not _NAME.match(name):
                raise self.err(["profile", i, "name"], "missing" if name is None else f"{name!r} is not a name",
                               "lower-case letters, digits and -, e.g. sky130 or ihp-sg13g2")
            if name in profiles:
                raise self.err(["profile", i, "name"], f"{name!r} is named by another [[profile]] too",
                               "give each process its own name")
            vs = t.get("variants")
            if not isinstance(vs, list) or not vs or not all(isinstance(x, str) for x in vs):
                raise self.err(["profile", i, "variants"], "missing" if vs is None else "not a list of strings",
                               'list the PDK variants it describes, e.g. variants = ["sky130A", "sky130B"]')
            for v in vs:
                if v in variants:
                    raise self.err(["profile", i, "variants"], f"{v} is a variant of [[profile]] {variants[v]!r} too",
                                   "list each variant in one [[profile]] only")
                if family_of(v) != family:
                    raise self.err(["profile", i, "variants"], f"{v} is not a variant of {family}",
                                   f"its ciel family is {family_of(v)}")
                variants[v] = name
            body = {k: x for k, x in t.items() if k not in _PROFILE_ONLY}
            verified, magic_drc = tested.flags(family, version, name)
            p = self.dataclass(Profile, body, ["profile", i],
                               given={"family": name, "verified": verified, "magic_drc": magic_drc},
                               refused=_UNDINE_SIDE)
            self.check(p, vs, ["profile", i])
            profiles[name] = p
        pdk = self.entries(doc.get("pdk", []), family, variants)
        return ProfileSet(package=package, version=version, family=family, undine=undine, profiles=profiles,
                          variants=variants, pdk=pdk)

    def entries(self, v, family: str, variants: dict) -> tuple[PdkEntry, ...]:
        from undine.store.manifest import _COMMIT, _TAG
        if not isinstance(v, list) or not all(isinstance(t, dict) for t in v):
            raise self.err(["pdk"], "not an array of tables", "write each commit as a [[pdk]] table")
        out, seen = [], set()
        for i, t in enumerate(v):
            for k in t:
                if k not in _PDK_KEYS:
                    raise self.unknown(["pdk", i, k], _PDK_KEYS)
            commit = t.get("commit")
            if not isinstance(commit, str) or not _COMMIT.match(commit):
                raise self.err(["pdk", i, "commit"],
                               "missing" if commit is None else f"{commit!r} is not a full commit",
                               "write the whole commit id: 40 or 64 lower-case hex characters")
            if commit in seen:
                raise self.err(["pdk", i, "commit"], f"{commit[:12]} is listed twice", "keep one [[pdk]] per commit")
            seen.add(commit)
            compat = self.value(t.get("compat", []), tuple[str, ...], ["pdk", i, "compat"])
            for j, c in enumerate(compat):
                fam, _, tag = c.partition("/")
                if fam != family or not _TAG.match(tag):
                    raise self.err(["pdk", i, "compat", j], f"{c!r} is not a tag of {family}",
                                   f"write {family}/<tag>, the tag lower-case letters, digits, '.', '_' and '-'")
            tested_ = self.value(t.get("tested", False), bool, ["pdk", i, "tested"])
            license = t.get("license")
            if license is not None and (not isinstance(license, str) or not license.strip()):
                raise self.err(["pdk", i, "license"], f"{license!r} is not a licence",
                               'write an SPDX id, e.g. "Apache-2.0"')
            tree = self.raw(t.get("tree", {}), ["pdk", i, "tree"])
            for var, parts in tree.items():
                if var not in variants:
                    raise self.err(["pdk", i, "tree", var], "not a variant of any [[profile]]",
                                   f"the variants are {', '.join(variants)}")
                if not isinstance(parts, dict):
                    raise self.err(["pdk", i, "tree", var], "not a table", "write [pdk.tree.<variant>]")
                for part, h in parts.items():
                    if _path_problem(part) or not isinstance(h, str) or not _SHA.match(h):
                        raise self.err(["pdk", i, "tree", var, part], "not a relative part and a tree hash",
                                       'write "libs.tech" = "sha256:<64 hex>"')
            out.append(PdkEntry(commit=commit, compat=compat, tested=tested_, license=license, tree=tree))
        return tuple(out)

    # ------------------------------------------------------------- checks beyond types

    def check(self, p: Profile, variants: list, path: list) -> None:
        if p.drc_switches not in SWITCH_MODES:
            raise self.err(path + ["drc_switches"], f"{p.drc_switches!r} is not a switch mode",
                           f"write one of {', '.join(SWITCH_MODES)}")
        for k in _PATHS:
            for j, x in _each(getattr(p, k)):
                if (problem := _path_problem(x)) or (k not in _TEMPLATES and ("{" in x or "}" in x)
                                                      and (problem := "holds a brace, which only a template has")):
                    raise self.err(path + [k] + j, f"{x!r} {problem}", "write a path relative to where it is read")
        for j, (name, rel) in enumerate(p.model_vars):
            if not re.fullmatch(r"[A-Za-z0-9_]+", name):     # a Tcl variable (GF180's 180MCU_MODELS)
                raise self.err(path + ["model_vars", j, 0], f"{name!r} is not a variable name",
                               "letters, digits and _")
            if (problem := _path_problem(rel)):
                raise self.err(path + ["model_vars", j, 1], f"{rel!r} {problem}", "write it relative to the PDK root")
        for j, x in _each(p.osdi):
            if (problem := _path_problem(x)) or "/" in x:
                raise self.err(path + ["osdi"] + j, f"{x!r} {problem or 'is not a file name'}",
                               "name the compiled model's file, with no directory")
        for k in ("corners",):
            for j, x in _each(getattr(p, k)):
                if not _WORD.match(x):
                    raise self.err(path + [k] + j, f"{x!r} is not a corner name", "letters, digits and _ . + -")
        for k, allowed in _TEMPLATES.items():
            for j, x in _each(getattr(p, k)):
                self.template(x, allowed, path + [k] + j)
        for j, el in enumerate(p.lvs_elements):
            self.regex(el.model, path + ["lvs_elements", j, "model"])
            for name, tol in el.check.items():
                if not _IDENT.match(name):
                    raise self.err(path + ["lvs_elements", j, "check", name], f"{name!r} is not a parameter name",
                                   "letters, digits and _")
                if not (math.isfinite(tol) and tol > 0):
                    raise self.err(path + ["lvs_elements", j, "check", name],
                                   f"{tol!r} is not a tolerance", "write a relative tolerance above 0 (0.005: 0.5 %)")
        for j, (pat, lib) in enumerate(p.stdcells):
            groups = self.regex(pat, path + ["stdcells", j, 0])
            self.library(lib, groups, path + ["stdcells", j, 1])
        if p.stdcell_lvs is not None:
            s, sp = p.stdcell_lvs, path + ["stdcell_lvs"]
            for k in ("mos", "short", "physical"):
                self.regex(getattr(s, k), sp + [k])
            for j, (pat, _) in enumerate(s.rename):
                self.regex(pat, sp + ["rename", j, 0])
        if p.pcell_lib is not None:
            self.pcell_lib(p.pcell_lib, path + ["pcell_lib"])
        for k in ("deck_vars", "stacks"):
            for v in getattr(p, k):
                if v not in variants:
                    raise self.err(path + [k, v], f"{v} is not among this profile's variants",
                                   f"its variants are {', '.join(variants)}")

    def template(self, text: str, allowed: set, path: list) -> None:
        try:
            parts = list(string.Formatter().parse(text))
        except ValueError as e:
            raise self.err(path, f"{text!r} is not a template: {e}", "write {{ and }} for a brace") from None
        for _, name, spec, conv in parts:
            if name is not None and (name not in allowed or spec or conv):
                raise self.err(path, f"{text!r}: {{{name}}} is not a placeholder of this field",
                               f"its placeholders are {', '.join('{' + a + '}' for a in sorted(allowed))}")

    def regex(self, text: str, path: list):
        try:
            return re.compile(text)
        except (re.error, OverflowError, RecursionError) as e:
            raise self.err(path, f"{text!r} is not a regular expression: {e}") from None

    def library(self, lib: str, pattern, path: list) -> None:
        """A stdcell library name, which may take groups of its cell pattern (\\1, \\g<name>): the groups must exist,
        and the name with them filled must stay a name (Profile.stdcell formats it into a path in the PDK)."""
        for num, name in _GROUP.findall(lib):
            ok = (int(num) <= pattern.groups) if num else (name in pattern.groupindex or
                                                            (name.isdigit() and int(name) <= pattern.groups))
            if not ok:
                raise self.err(path, f"{lib!r} names a group its pattern {pattern.pattern!r} does not have")
        if not _WORD.match(_GROUP.sub("x", lib)) or lib in (".", ".."):
            raise self.err(path, f"{lib!r} is not a library name",
                           "letters, digits and _ . + -, or a group of the pattern")

    def pcell_lib(self, lib: PcellLib, path: list) -> None:
        for j, x in enumerate(lib.paths):
            if (problem := _path_problem(x)):
                raise self.err(path + ["paths", j], f"{x!r} {problem}", "write it relative to libs.tech/klayout")
        if not lib.paths:
            raise self.err(path + ["paths"], "empty", "name the directory that holds the module")
        for k in ("module", "init", "library", "technology"):
            x = getattr(lib, k)
            if x is not None and not _DOTTED.match(x):
                raise self.err(path + [k], f"{x!r} is not a dotted name", "letters, digits and _, parts joined by .")
        if lib.env not in PCELL_ENVS:
            raise self.err(path + ["env"], f"{lib.env!r} is not a PCell environment of this Undine",
                           f"name one of {', '.join(PCELL_ENVS)}")
        for j, (mod, attr, _) in enumerate(lib.limits):
            if not _DOTTED.match(mod) or not _IDENT.match(attr):
                raise self.err(path + ["limits", j], f"{mod!r}, {attr!r} are not a module and an attribute",
                               "write [module, attribute, number]")


@functools.cache
def _hints(cls) -> dict:
    return typing.get_type_hints(cls)


def _each(v):
    """(index path, string) of a field that is a string or a tuple of strings; none for None."""
    if isinstance(v, str):
        return [([], v)]
    if isinstance(v, tuple):
        return [([i], x) for i, x in enumerate(v)]
    return []


def _path_problem(p: str) -> str | None:
    if p.startswith(("/", "~")) or "\\" in p or re.match(r"^[A-Za-z]:", p):
        return "is not a relative / path"
    if _UNSAFE.search(p) or _CONTROL.search(p):
        return "holds whitespace or a character a netlist or Tcl reads as syntax"

    if ".." in p.split("/"):
        return "has a .. part"
    return None


# ------------------------------------------------------------------ loading


def _undine_version() -> str | None:
    from undine import tools
    v = tools._package_version(tools._repo())
    return None if v == "unknown" else v


def load(text: str, where: str, package: str, version: str, undine_version: str | None = None) -> ProfileSet:
    """The profile.toml `text` of package `package` at `version`, checked against this Undine (or `undine_version`).
    Raises ProfileError for the first problem found."""
    try:
        doc = tomllib.loads(text)
    except (RecursionError, MemoryError):
        raise ProfileError(f"{where}: {PROFILE_TOML}: nested too deep or too large to read: write it flat") from None
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"{where}: {PROFILE_TOML}: not valid TOML: {e}: fix the TOML at that line") from None
    return _Reader(where).document(doc, package, version, undine_version or _undine_version())


def load_dir(d: Path, where: str | None = None, undine_version: str | None = None) -> ProfileSet:
    """The profile package in directory `d`: its manifest's name (profile_*) and version, and its profile.toml."""
    from undine.store import manifest
    where = where or Path(d).name
    m = manifest.from_dir(Path(d), where)
    if m is None:
        raise ProfileError(f"{where}: no {manifest.MANIFEST}: a profile package has one, naming it and its version")
    if not m.name.startswith(PREFIX):
        raise ProfileError(f"{where}: {manifest.MANIFEST}: package.name: {m.name!r} is not a profile package's name: "
                           f"profile packages are named {PREFIX}<family>")
    p = Path(d) / PROFILE_TOML
    if p.is_symlink() or not p.is_file():
        raise ProfileError(f"{where}: no {PROFILE_TOML} (a regular file) next to its {manifest.MANIFEST}")
    return load(p.read_text(encoding="utf-8"), where, m.name, str(m.version), undine_version)


@functools.cache
def builtin() -> tuple[ProfileSet, ...]:
    """The profile packages this Undine ships, loaded once."""
    return tuple(load_dir(BUILTIN / name, f"built-in {name}") for name in BUILTIN_PACKAGES)


def builtin_for(family: str | None) -> ProfileSet | None:
    """The built-in profile package of a ciel family."""
    return next((s for s in builtin() if s.family == family), None)


def tested_commits(family: str | None) -> tuple[str, ...]:
    """The PDK commits of a ciel family that Undine's tests run on, from the built-in profiles (install-time facts:
    pick_pdk, setup.sh, `pixi run pdk`); the first is the one to install. () for a family with no built-in profile."""
    s = builtin_for(family)
    return s.tested_commits() if s else ()
