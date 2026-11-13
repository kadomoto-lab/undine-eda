"""undine-package.toml: a package's manifest, parsed and checked (docs/design/packages.md, Packages).

Pure: no git, no network, no writes. Every error reads `<where>: undine-package.toml: <key path>: <what>: <fix>`,
where `where` names the package or the requirement that led to it, so one refusal can list the problems of several
manifests.

Until the package index (phase 3) every dependency a manifest names is a git requirement:

    [dependencies]
    sar_common = { git = "https://example.org/sar.git", rev = "v1.3.1", version = "^1.3" }
    bias = { git = "https://example.org/bias.git", rev = "v0.3.2", subdir = "lib", package = "bias_gen" }
    common = { subdir = "common" }       # a sibling: same repository and commit, subdir from the repository root

Index forms (a string, or a table with neither git nor subdir) are kept as IndexDep so that a manifest written for
phase 3 parses; the resolver refuses them.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:                  # Python 3.10: the same parser, packaged as tomli
    import tomli as tomllib

from undine.store import cas
from undine.store.semver import Req, SemverError, Version

MANIFEST = "undine-package.toml"
NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_BYTES = 256 * 1024

_FAMILY = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_VARIANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_HEXISH = re.compile(r"^[0-9a-fA-F]+$")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
CELL_SUFFIXES = (".sch", ".sym", ".oas", ".sv", ".v")
_EXPORT_SUFFIXES = (".sym", ".sch", ".oas")

TABLES = ("package", "pdk", "dependencies", "exports", "files", "checks", "renames")
_KEYS = {
    "package": ("name", "version", "description", "license", "authors", "repository"),
    "pdk": ("family", "variants", "commits", "compat", "built_with"),
    "exports": ("cells",),
    "files": ("include",),
}
_DEP_KEYS = ("git", "rev", "subdir", "package", "version", "index")
_NAME_FIX = "lower-case letters, digits and _, starting with a letter, at most 64 characters"


class ManifestError(ValueError):
    pass


@dataclass
class PdkReq:
    family: str
    variants: tuple = ()
    commits: tuple = ()
    compat: tuple = ()
    built_with: str | None = None


@dataclass
class GitDep:
    """git None: a sibling in the requirer's repository and commit, its subdir taken from the repository root."""
    local: str
    package: str
    git: str | None
    rev: str | None
    subdir: str | None
    version: Req | None = None


@dataclass
class IndexDep:
    """A requirement in the package index's form (phase 3). Parsed so a newer manifest reads; resolution refuses it."""
    local: str
    package: str
    version: str
    index: str | None = None


@dataclass
class Manifest:
    name: str
    version: Version
    description: str | None = None
    license: str | None = None
    authors: tuple = ()
    repository: str | None = None
    pdk: PdkReq | None = None
    dependencies: dict = field(default_factory=dict)       # {local name: GitDep | IndexDep}
    exports: tuple | None = None                           # None: every cell is public
    include: tuple | None = None                           # [files] include; None: every file
    later: dict = field(default_factory=dict)              # [checks], [renames]: kept as written until phase 5


# ------------------------------------------------------------------ errors


def _kp(*parts) -> str:
    """A TOML key path: package.version, dependencies.bias.git, renames."1.2.0"."""
    return ".".join(p if _BARE_KEY.match(p) else '"' + p.replace('"', '\\"') + '"' for p in map(str, parts))


def _err(where: str, path: str, what: str, fix: str = "") -> ManifestError:
    return ManifestError(": ".join(x for x in (where, MANIFEST, path, what, fix) if x))


# ------------------------------------------------------------------ parse


def parse(data: bytes, where: str) -> Manifest:
    """The manifest in `data`, checked. Raises ManifestError for the first problem found."""
    if len(data) > MAX_BYTES:
        raise _err(where, "", f"{len(data)} bytes, more than {MAX_BYTES}", "a manifest is a short text file")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _err(where, "", f"not UTF-8 (byte {e.start})", "save it as UTF-8") from None
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise _err(where, "", f"not valid TOML: {e}", "fix the TOML at that line") from None
    return _Parser(where).manifest(doc)


class _Parser:
    def __init__(self, where: str):
        self.where = where

    def err(self, path: str, what: str, fix: str = "") -> ManifestError:
        return _err(self.where, path, what, fix)

    def known(self, table: dict, keys: tuple, *path) -> None:
        for k in table:
            if k not in keys:
                raise self.err(_kp(*path, k), "unknown key (made for a newer Undine?)",
                               f"the known keys are {', '.join(keys)}")

    def table(self, doc: dict, key: str) -> dict | None:
        v = doc.get(key)
        if v is not None and not isinstance(v, dict):
            raise self.err(_kp(key), "not a table", f"write it as [{key}]")
        return v

    def string(self, t: dict, key: str, *path, required: bool = False) -> str | None:
        v = t.get(key)
        if v is None:
            if required:
                raise self.err(_kp(*path, key), "missing", f"add {key} = \"...\"")
            return None
        if not isinstance(v, str):
            raise self.err(_kp(*path, key), f"{v!r} is not a string", "quote it")
        return v

    def strings(self, t: dict, key: str, *path) -> tuple | None:
        v = t.get(key)
        if v is None:
            return None
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise self.err(_kp(*path, key), "not a list of strings", f'write {key} = ["...", "..."]')
        return tuple(dict.fromkeys(v))

    def manifest(self, doc: dict) -> Manifest:
        for k in doc:
            if k not in TABLES:
                raise self.err(_kp(k), "unknown table (made for a newer Undine?)",
                               f"the known tables are {', '.join(TABLES)}")
        pkg = self.table(doc, "package")
        if pkg is None:
            raise self.err("package", "missing", "add a [package] table with name and version")
        self.known(pkg, _KEYS["package"], "package")
        name = self.name(pkg.get("name"), "package", "name")
        try:
            version = Version.parse(self.string(pkg, "version", "package", required=True))
        except SemverError as e:
            raise self.err("package.version", str(e)) from None
        m = Manifest(name=name, version=version, description=self.string(pkg, "description", "package"),
                     license=self.string(pkg, "license", "package"),
                     authors=self.strings(pkg, "authors", "package") or (),
                     repository=self.repository(pkg))
        m.pdk = self.pdk(self.table(doc, "pdk"))
        m.dependencies = self.dependencies(self.table(doc, "dependencies") or {}, name)
        m.exports = self.exports(self.table(doc, "exports"))
        m.include = self.include(self.table(doc, "files"))
        for k in ("checks", "renames"):
            if (t := self.table(doc, k)) is not None:
                m.later[k] = t
        return m

    def name(self, v, *path) -> str:
        if v is None:
            raise self.err(_kp(*path), "missing", f"add {path[-1]} = \"...\" ({_NAME_FIX})")
        if not isinstance(v, str) or not NAME.match(v):
            raise self.err(_kp(*path), f"{v!r} is not a package name", _NAME_FIX)
        return v

    def url(self, v, *path) -> str:
        from undine.store import deps                    # deps imports this module
        if not isinstance(v, str) or v.startswith("-") or not deps._URL.match(v):
            raise self.err(_kp(*path), f"{v!r} is not a git URL",
                           "write https://..., ssh://..., git://... or user@host:path")
        return v

    def repository(self, pkg: dict) -> str | None:
        v = pkg.get("repository")
        return None if v is None else self.url(v, "package", "repository")

    def commit(self, v, *path) -> str:
        if not isinstance(v, str) or not _COMMIT.match(v):
            what = (f"{v!r} is a prefix" if isinstance(v, str) and _HEXISH.match(v) and len(v) < 40
                    else f"{v!r} is not a full commit")
            raise self.err(_kp(*path), what, "write the whole commit id: 40 or 64 lower-case hex characters")
        return v

    def pdk(self, t: dict | None) -> PdkReq | None:
        if t is None:
            return None
        from undine.pdks.pdk import family_of
        self.known(t, _KEYS["pdk"], "pdk")
        family = self.string(t, "family", "pdk", required=True)
        if not _FAMILY.match(family):
            raise self.err("pdk.family", f"{family!r} is not a PDK family",
                           "write ciel's family name, e.g. sky130, gf180mcu or ihp-sg13")
        variants = self.strings(t, "variants", "pdk")
        if not variants:
            raise self.err("pdk.variants", "missing or empty", 'list the variants the cells are for, e.g. ["sky130A"]')
        for v in variants:
            if not _VARIANT.match(v) or family_of(v) != family:
                raise self.err("pdk.variants", f"{v!r} is not a variant of {family}",
                               f"list only variants of family {family}, or change pdk.family")
        commits = tuple(self.commit(c, "pdk", "commits") for c in self.strings(t, "commits", "pdk") or ())
        compat = self.strings(t, "compat", "pdk") or ()
        for c in compat:
            fam, _, tag = c.partition("/")
            if fam != family or not _TAG.match(tag):
                raise self.err("pdk.compat", f"{c!r} is not a compatibility tag of {family}",
                               f'write "{family}/<tag>", the tag in lower-case letters, digits and . _ -')
        built = t.get("built_with")
        if built is not None:
            built = self.commit(built, "pdk", "built_with")
            if commits and not compat and built not in commits:
                raise self.err("pdk.built_with", "not one of pdk.commits",
                               "add it to commits: the PDK the layouts were checked on is compatible")
        return PdkReq(family, variants, commits, compat, built)

    def dependencies(self, t: dict, own: str) -> dict:
        return {local: self.dependency(local, v, own) for local, v in t.items()}

    def dependency(self, local: str, v, own: str):
        path = ("dependencies", local)
        if not NAME.match(local):
            raise self.err(_kp(*path), f"{local!r} is not a local name", _NAME_FIX)
        if local == own:
            raise self.err(_kp(*path), "the package depends on itself",
                           f"drop it: the package's own cells are placed as {own}/<cell>.sym already")
        if isinstance(v, str):
            return IndexDep(local, local, v)
        if not isinstance(v, dict):
            raise self.err(_kp(*path), f"{v!r} is neither a version nor a table",
                           'write { git = "...", rev = "..." }')
        self.known(v, _DEP_KEYS, *path)
        package = self.name(v.get("package", local), *path, "package")
        if package == own:
            raise self.err(_kp(*path, "package"), "the package depends on itself", "name another package")
        if "rev" in v and "git" not in v:
            raise self.err(_kp(*path, "rev"), "rev without git",
                           "add git = \"...\", or drop rev for a sibling in the same repository")
        if "index" in v and ("git" in v or "subdir" in v):
            raise self.err(_kp(*path, "index"), "index together with git or subdir",
                           "give either a git requirement or an index one")
        if "index" in v or ("git" not in v and "subdir" not in v):
            version = self.string(v, "version", *path) or "*"
            return IndexDep(local, package, version, self.string(v, "index", *path))
        git = rev = None
        if "git" in v:
            git = self.url(v["git"], *path, "git")
            rev = self.rev(v.get("rev"), *path, "rev")
        sub = self.subdir(v.get("subdir"), *path, "subdir")
        version = None
        if "version" in v:
            try:
                version = Req.parse(v["version"])
            except SemverError as e:
                raise self.err(_kp(*path, "version"), str(e)) from None
        return GitDep(local, package, git, rev, sub, version)

    def rev(self, v, *path) -> str:
        from undine.store import deps
        if v is None:
            raise self.err(_kp(*path), "missing", 'name a tag, branch or commit, e.g. rev = "v1.2.0"')
        if not isinstance(v, str) or not deps._REV.match(v):
            raise self.err(_kp(*path), f"{v!r} is not a branch, tag or commit",
                           "name one, e.g. v1.2.0, main or a commit; it does not start with -")
        return v

    def subdir(self, v, *path) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str) or not v:
            raise self.err(_kp(*path), f"{v!r} is not a directory", 'write a relative path, e.g. "lib"')
        parts = v.split("/")
        if v.startswith("/") or "\\" in v or any(p in ("", "..") or p.startswith("-") for p in parts):
            raise self.err(_kp(*path), f"{v!r} is not a relative path inside the repository",
                           "write it like lib/common: no leading /, no .., no part starting with -")
        return v

    def exports(self, t: dict | None) -> tuple | None:
        if t is None:
            return None
        from undine.store.project import NAME_RE
        self.known(t, _KEYS["exports"], "exports")
        cells = self.strings(t, "cells", "exports") or ()
        for c in cells:
            if not NAME_RE.match(c):
                raise self.err("exports.cells", f"{c!r} is not a cell name",
                               "letters, digits and _ . + -, not starting with . + or -")
        return cells

    def include(self, t: dict | None) -> tuple | None:
        if t is None:
            return None
        self.known(t, _KEYS["files"], "files")
        pats = self.strings(t, "include", "files")
        if pats is None:
            raise self.err("files.include", "missing", 'list the files, e.g. include = ["*.sch", "*.sym"]')
        for p in pats:
            if (problem := _pattern_problem(p)) is not None:
                raise self.err("files.include", f"{p!r} {problem[0]}", problem[1])
        return pats


# ------------------------------------------------------------------ files


def _pattern_problem(p: str) -> tuple[str, str] | None:
    """(what, fix) when an include pattern is malformed, else None."""
    if not p:
        return "is empty", "drop it"
    if p.startswith("/") or "\\" in p:
        return "is not a relative / path", "write it relative to the package's directory, with /"
    for seg in p.split("/"):
        if seg == "":
            return "has an empty part", "write a directory's files as dir/**"
        if seg == "..":
            return "has a .. part", "patterns match only inside the package"
        if "**" in seg and seg != "**":
            return "has ** inside a part", "** stands alone between slashes (rtl/**), * matches within a part"
    return None


def _match(pat: list[str], path: list[str]) -> bool:
    if not pat:
        return not path
    if pat[0] == "**":
        return any(_match(pat[1:], path[i:]) for i in range(len(path) + 1))
    return bool(path) and fnmatch.fnmatchcase(path[0], pat[0]) and _match(pat[1:], path[1:])


def select(include, paths) -> set[str]:
    """The paths (POSIX, below the package's subdir) an include list selects. None selects every path. A pattern
    matches the whole path; * ? and [..] stay within one part, ** is a whole part matching zero or more parts. The
    manifest is always selected."""
    paths = list(paths)
    if include is None:
        return set(paths)
    pats = []
    for p in include:
        if (problem := _pattern_problem(p)) is not None:
            raise ManifestError(f"{MANIFEST}: files.include: {p!r} {problem[0]}: {problem[1]}")
        pats.append(p.split("/"))
    return {x for x in paths if x == MANIFEST or any(_match(pat, x.split("/")) for pat in pats)}


def check_files(m: Manifest, paths, where: str) -> None:
    """Refuses a selection with no top-level cell, and exported cells the selection does not have."""
    sel = select(m.include, paths)
    top = {p for p in sel if "/" not in p}
    if not any(p.endswith(CELL_SUFFIXES) for p in top):
        if m.include is None:
            raise _err(where, "", "no top-level cell (.sch, .sym, .oas, .sv or .v)",
                       "a package is a library: put its cells at the top of its directory")
        raise _err(where, "files.include", "selects no top-level cell (.sch, .sym, .oas, .sv or .v)",
                   'add patterns such as "*.sch" and "*.sym"; *.sch matches top-level cells only')
    missing = [c for c in m.exports or () if not any(c + s in top for s in _EXPORT_SUFFIXES)]
    if missing:
        raise _err(where, "exports.cells", f"no .sym, .sch or .oas among the package's files for {', '.join(missing)}",
                   "add the cell's files to [files] include, or drop it from exports")


# ------------------------------------------------------------------ reading


def _read(p: Path) -> bytes:
    """At most MAX_BYTES + 1 bytes: enough for parse to refuse a larger file without reading all of it."""
    with open(p, "rb") as f:
        return f.read(MAX_BYTES + 1)


def from_files(files: dict, where: str) -> Manifest | None:
    """The manifest of a tree's files ({path: (mode, hex)}), read from the store; None when it has none. The object
    is re-hashed before its bytes are parsed."""
    entry = files.get(MANIFEST)
    if entry is None:
        return None
    hex = entry[1]
    if not cas.check_object(hex, repair_mode=True):
        raise _err(where, "", "the store's copy is missing or damaged",
                   "run `undine store verify`, then `undine lib sync` to read it again")
    data = _read(cas.object_path(hex))
    if len(data) <= MAX_BYTES and hashlib.sha256(data).hexdigest() != hex:
        raise _err(where, "", "the store's copy changed while it was read", "run `undine store verify`")
    return parse(data, where)


def from_tree(tree_id: str, where: str) -> Manifest | None:
    """The manifest of a recorded tree; None when the tree has no manifest or the store has no record of it."""
    files = cas.read_tree(tree_id)
    return None if files is None else from_files(files, where)


def from_archive(sha: str, where: str) -> Manifest | None:
    """The same for a lock format 1 entry's archive record."""
    files = cas.read_archive_tree(sha)
    return None if files is None else from_files(files, where)


def from_dir(d: Path, where: str) -> Manifest | None:
    """The manifest of an installed or checked-out directory; None when it has none. A manifest that is a symbolic
    link is refused: it could name any file on the machine."""
    p = Path(d) / MANIFEST
    if p.is_symlink():
        raise _err(where, "", "is a symbolic link", "a manifest is a regular file; reinstall with `undine lib sync`")
    if not p.exists():
        return None
    if not p.is_file():
        raise _err(where, "", "is not a regular file", "make it one")
    return parse(_read(p), where)
