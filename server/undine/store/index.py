"""Package indexes (docs/design/packages.md, Index and Resolution 2-3): git repositories with one JSON line per version
of a package, read from a local copy, and the choice of one version per package that every requirement accepts.

An index is a git repository:

    config.json                      {"name": "undine", "format": 1}
    an/al/analog_ip.jsonl            one line per version, appended, never rewritten (except "yanked")
    3/i/inv.jsonl                    names of three letters: 3/<first letter>/
    2/op.jsonl                       names of one or two letters: 1/ or 2/

Each index is fetched into store/index/<sha256 of URL>.git, a bare repository whose ref refs/undine/index is the
index as last fetched; it is fetched on first use and then only on request (`pkg index update`, `pkg update`, or a
resolution that finds no version it needs, once). Every file is read from that ref with git cat-file, never checked
out. An index is known by its URL; a nickname (`undine`, or one from $UNDINE_HOME/config.toml or project.json's
`indexes`) is local to the machine or the project.

solve() chooses the versions: for each (index URL, package) the highest version that is not yanked (unless the lock
has it), fits the project's PDK and that every requirement on it accepts, its own dependencies resolving too
(depth-first with backtracking: the graphs are small). One version per package name, and one index per package name.
It is pure: the deps layer gives it the index lines and turns its choice into git requirements pinned at the commits
the index names (store/deps.py), which store/resolve.py walks as any other."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from undine.home import home, store_root
from undine.store import gitsrc
from undine.store import manifest as mf
from undine.store.semver import Req, SemverError, Version

DEFAULT = "undine"
DEFAULT_URL = "https://github.com/kadomoto-lab/undine-index.git"
FORMAT = 1
REF = "refs/undine/index"
MAX_LINE = 64 * 1024                     # one version's line; a package file is a few of them
MAX_FILE = 16 * 1024 * 1024
_STEPS = 50_000                          # solve's search: far above any real graph, a bound on a pathological one
_HEX = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_NICK = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_LINE_KEYS = {"name", "vers", "git", "subdir", "commit", "tree", "pdk", "deps", "exports", "license", "size",
              "check", "yanked", "description"}
_DEP_KEYS = {"name", "package", "req", "index"}


class IndexReadError(Exception):
    """An index that cannot be read, or a line or a requirement that is not what the index format says."""


# ------------------------------------------------------------------ names and URLs


def rel_path(name: str) -> str:
    """The file of package `name` in an index, after crates.io-index: 1/<n>, 2/<n>, 3/<first letter>/<n>, else
    <first two letters>/<next two>/<n>, with .jsonl."""
    if not mf.NAME.match(name or ""):
        raise IndexReadError(f"{name!r} is not a package name ({mf._NAME_FIX})")
    if len(name) <= 2:
        return f"{len(name)}/{name}.jsonl"
    if len(name) == 3:
        return f"3/{name[0]}/{name}.jsonl"
    return f"{name[:2]}/{name[2:4]}/{name}.jsonl"


def is_url(s: str) -> bool:
    """A repository URL or path rather than an index nickname."""
    return isinstance(s, str) and ("://" in s or s.startswith(("/", "~", ".")) or re.match(r"^[\w.-]+@[\w.-]+:", s)
                                   is not None)


def _check_url(url) -> str:
    if not isinstance(url, str) or not is_url(url) or url.startswith("-") or "\0" in url or "\n" in url:
        raise IndexReadError(f"not an index URL: {url!r} (a git URL such as https://github.com/acme/undine-index.git)")
    return url


def configured() -> dict[str, str]:
    """{nickname: URL} of this machine: `undine` (the default index), then $UNDINE_HOME/config.toml's [indexes]
    (which may give `undine` another URL, a mirror)."""
    out = {DEFAULT: DEFAULT_URL}
    p = home() / "config.toml"
    if p.is_file():
        try:
            data = tomllib.loads(p.read_text())
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise IndexReadError(f"{p}: not TOML: {e}") from None
        t = data.get("indexes", {})
        if not isinstance(t, dict):
            raise IndexReadError(f"{p}: [indexes] is not a table of nickname = \"URL\"")
        for k, v in t.items():
            if not _NICK.match(k):
                raise IndexReadError(f"{p}: indexes.{k}: not a nickname (lower-case letters, digits, _ and -)")
            out[k] = _check_url(v)
    return out


def project_indexes(meta: dict) -> dict[str, str]:
    """project.json's `indexes`, {nickname: URL}, checked."""
    t = meta.get("indexes", {}) if isinstance(meta, dict) else {}
    if not isinstance(t, dict):
        raise IndexReadError("project.json: indexes is not an object of nickname: URL")
    out = {}
    for k, v in t.items():
        if not (isinstance(k, str) and _NICK.match(k)):
            raise IndexReadError(f"project.json: indexes.{k}: not a nickname (lower-case letters, digits, _ and -)")
        try:
            out[k] = _check_url(v)
        except IndexReadError as e:
            raise IndexReadError(f"project.json: indexes.{k}: {e}") from None
    return out


def known(meta: dict | None = None) -> dict[str, str]:
    """Every nickname a project can use: this machine's, then project.json's (which win: a clone of the project
    finds the same indexes)."""
    return configured() | project_indexes(meta or {})


def url_of(name: str | None, meta: dict | None = None, default: str | None = None) -> str:
    """The URL an `index` value means: a URL as written, a nickname looked up, None the `default` (else the default
    index's URL)."""
    if name is None:
        return default or known(meta)[DEFAULT]
    if is_url(name):
        return _check_url(name)
    k = known(meta)
    if name not in k:
        raise IndexReadError(f"no index is named {name!r} (known: {', '.join(sorted(k))}): add it to project.json's "
                          f"\"indexes\" or to {home() / 'config.toml'} [indexes], or give its URL")
    return k[name]


def nickname(url: str, meta: dict | None = None) -> str:
    """The nickname of `url` here, else the URL itself."""
    for k, v in known(meta).items():
        if v == url:
            return k
    return url


# ------------------------------------------------------------------ lines


@dataclass(frozen=True)
class Dep:
    """One requirement of an index line: local name, package, requirement, and the index URL (None: the line's
    own index, cargo's rule)."""
    local: str
    package: str
    req: Req
    index: str | None = None

    def text(self) -> str:
        alias = f" as {self.local}" if self.local != self.package else ""
        return f"{self.package} {self.req}{alias}"


@dataclass(frozen=True)
class Line:
    """One version of a package in an index (`index`: its URL)."""
    name: str
    vers: Version
    git: str
    commit: str
    tree: str
    index: str = ""
    subdir: str | None = None
    pdk: dict | None = None
    deps: tuple[Dep, ...] = ()
    exports: tuple[str, ...] | None = None
    license: str | None = None
    size: int | None = None
    check: dict | None = None
    yanked: bool = False
    description: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name} {self.vers}"

    def pdk_req(self) -> mf.PdkReq | None:
        if not self.pdk:
            return None
        p = self.pdk
        return mf.PdkReq(p["family"], tuple(p.get("variants") or ()), tuple(p.get("commits") or ()),
                         tuple(p.get("compat") or ()), p.get("built_with"))


def parse_line(text: str, where: str, index: str = "") -> Line:
    """One JSON line of a package file, checked: what a later step hands to git (git, subdir, commit) and to the
    store (tree) is refused here unless it is exactly what the format allows."""
    if len(text) > MAX_LINE:
        raise IndexReadError(f"{where}: a line of {len(text)} bytes, more than {MAX_LINE}")
    try:
        d = json.loads(text)
    except json.JSONDecodeError as e:
        raise IndexReadError(f"{where}: not JSON: {e.msg}") from None
    if not isinstance(d, dict):
        raise IndexReadError(f"{where}: not a JSON object")
    unknown = sorted(set(d) - _LINE_KEYS)
    if unknown:
        raise IndexReadError(f"{where}: unknown key {unknown[0]!r} (made by a newer Undine?)")

    def string(k: str, required: bool = True) -> str | None:
        v = d.get(k)
        if v is None and not required:
            return None
        if not isinstance(v, str) or not v:
            raise IndexReadError(f"{where}: {k} is {'missing' if v is None else 'not a string'}")
        return v
    name = string("name")
    if not mf.NAME.match(name):
        raise IndexReadError(f"{where}: name {name!r} is not a package name")
    try:
        vers = Version.parse(string("vers"))
    except SemverError as e:
        raise IndexReadError(f"{where}: vers: {e}") from None
    git = string("git")
    try:
        gitsrc._check_url(git)
        sub = string("subdir", False)
        sub = gitsrc._subdir(sub) or None if sub is not None else None
    except gitsrc.GitSourceError as e:
        raise IndexReadError(f"{where}: {e}") from None
    commit = string("commit")
    if not _COMMIT.match(commit):
        raise IndexReadError(f"{where}: commit {commit!r} is not a full commit id")
    tree = string("tree")
    if not (tree.startswith("sha256:") and _HEX.match(tree[7:])):
        raise IndexReadError(f"{where}: tree {tree!r} is not a tree hash (sha256:<64 hex>)")
    pdk = d.get("pdk")
    if pdk is not None:
        if not (isinstance(pdk, dict) and isinstance(pdk.get("family"), str)):
            raise IndexReadError(f"{where}: pdk is not an object with a family")
        for k in ("variants", "commits", "compat"):
            if not (pdk.get(k) is None or isinstance(pdk[k], list) and all(isinstance(x, str) for x in pdk[k])):
                raise IndexReadError(f"{where}: pdk.{k} is not a list of strings")
    deps = []
    for i, x in enumerate(d.get("deps") or []):
        at = f"{where}: deps[{i}]"
        if not isinstance(x, dict) or set(x) - _DEP_KEYS:
            raise IndexReadError(f"{at}: not an object of name, package, req and index")
        local, pkg = x.get("name"), x.get("package", x.get("name"))
        if not (isinstance(local, str) and mf.NAME.match(local) and isinstance(pkg, str) and mf.NAME.match(pkg)):
            raise IndexReadError(f"{at}: name and package must be package names")
        try:
            req = Req.parse(x.get("req", "*"))
        except SemverError as e:
            raise IndexReadError(f"{at}: req: {e}") from None
        idx = x.get("index")
        if idx is not None:
            try:
                idx = _check_url(idx)
            except IndexReadError as e:
                raise IndexReadError(f"{at}: {e}") from None
        deps.append(Dep(local, pkg, req, idx))
    exports = d.get("exports")
    if exports is not None and not (isinstance(exports, list) and all(isinstance(x, str) for x in exports)):
        raise IndexReadError(f"{where}: exports is not a list of cell names")
    size = d.get("size")
    if size is not None and not (isinstance(size, int) and not isinstance(size, bool) and size >= 0):
        raise IndexReadError(f"{where}: size is not a number of bytes")
    check = d.get("check")
    if check is not None and not (isinstance(check, dict) and isinstance(check.get("url"), str)):
        raise IndexReadError(f"{where}: check is not an object with a url")
    yanked = d.get("yanked", False)
    if not isinstance(yanked, bool):
        raise IndexReadError(f"{where}: yanked is not true or false")
    lic = d.get("license")
    desc = d.get("description")
    return Line(name, vers, git, commit, tree, index, sub, pdk, tuple(deps),
                tuple(exports) if exports is not None else None, lic if isinstance(lic, str) else None, size, check,
                yanked, desc if isinstance(desc, str) else None)


def parse_file(data: bytes, name: str, where: str, index: str = "") -> list[Line]:
    """The lines of one package file. Every version once (the first of two lines for one version stands: a yank is
    a rewrite of that line, never a second line); a line naming another package is refused."""
    if len(data) > MAX_FILE:
        raise IndexReadError(f"{where}: {len(data)} bytes, more than {MAX_FILE}")
    out: dict[Version, Line] = {}
    for n, raw in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        if not raw.strip():
            continue
        line = parse_line(raw, f"{where}:{n}", index)
        if line.name != name:
            raise IndexReadError(f"{where}:{n}: a line for {line.name} in the file of {name}")
        if line.vers in out:
            raise IndexReadError(f"{where}:{n}: {name} {line.vers} is listed twice")
        out[line.vers] = line
    return sorted(out.values(), key=lambda x: x.vers, reverse=True)


# ------------------------------------------------------------------ local copies


def _dir(url: str) -> Path:
    return store_root() / "index" / f"{hashlib.sha256(url.encode('utf-8', 'surrogateescape')).hexdigest()}.git"


@contextlib.contextmanager
def _lock(repo: Path, shared: bool):
    repo.parent.mkdir(parents=True, exist_ok=True)
    with open(repo.with_name(repo.name + ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Index:
    """The local copy of one index, by URL. `source`: what git fetches from (over HTTP, deps._source's checked git
    directory for a local index), else the URL."""

    def __init__(self, url: str, source: str | None = None):
        self.url = _check_url(url)
        self.source = source
        self.repo = _dir(self.url)
        self._files: dict[str, list[Line]] = {}
        self._names: list[str] | None = None

    # -- the copy

    def fetched(self) -> bool:
        return (self.repo / "HEAD").is_file() and self._commit() is not None

    def _commit(self) -> str | None:
        try:
            return gitsrc._git(self.repo, "rev-parse", "--verify", "--quiet", f"{REF}^{{commit}}") or None
        except gitsrc.GitSourceError:
            return None

    def commit(self) -> str | None:
        """The index commit read, None before the first fetch."""
        return self._commit() if (self.repo / "HEAD").is_file() else None

    def update(self) -> str:
        """Fetches the index's HEAD into the local copy; returns its commit. The index must say format 1. A fetch past
        UNDINE_FETCH_MAX is stopped and the local copy removed (gitsrc._fetch_git)."""
        src = self.source or gitsrc._plain(self.url)
        with _lock(self.repo, shared=False):
            if not (self.repo / "HEAD").is_file():
                shutil.rmtree(self.repo, ignore_errors=True)
                tmp = Path(tempfile.mkdtemp(dir=self.repo.parent, prefix=".new-"))
                try:
                    gitsrc._git(None, "init", "-q", "--bare", "--template=", str(tmp))
                    os.replace(tmp, self.repo)
                except BaseException:
                    shutil.rmtree(tmp, ignore_errors=True)
                    raise
            gitsrc._clear_stale_locks(self.repo)
            try:
                gitsrc._fetch_git(self.repo, self.url, "--no-tags", "--end-of-options", src, "HEAD")
                got = gitsrc._git(self.repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
            except gitsrc.GitSourceError as e:
                raise IndexReadError(f"index {self.url}: {e}") from None
            cfg = self._read(got, "config.json")
            try:
                fmt = json.loads(cfg).get("format") if cfg is not None else None
            except (json.JSONDecodeError, AttributeError):
                fmt = None
            if fmt != FORMAT:
                raise IndexReadError(f"index {self.url}: its config.json says format {fmt!r}, this Undine reads format "
                                  f"{FORMAT}" + (" (made by a newer Undine?)" if isinstance(fmt, int) and fmt > FORMAT
                                                 else ": is it a package index?"))
            gitsrc._git(self.repo, "update-ref", REF, got)
        self._files.clear()
        self._names = None
        return got

    def ensure(self) -> None:
        """Fetches the index once, when there is no local copy yet."""
        if not self.fetched():
            self.update()

    def _read(self, commit: str, path: str) -> bytes | None:
        try:
            return gitsrc._run(self.repo, "cat-file", "blob", f"{commit}:{path}")
        except gitsrc.GitSourceError:
            return None

    # -- reading

    def versions(self, name: str) -> list[Line]:
        """Every version of package `name` the local copy lists, newest first ([] when it lists none)."""
        if name not in self._files:
            self.ensure()
            path = rel_path(name)
            with _lock(self.repo, shared=True):
                data = self._read(REF, path)
            self._files[name] = [] if data is None else parse_file(data, name, f"{self.url}:{path}", self.url)
        return self._files[name]

    def names(self) -> list[str]:
        """Every package name the local copy lists."""
        if self._names is None:
            self.ensure()
            with _lock(self.repo, shared=True):
                out = gitsrc._git(self.repo, "ls-tree", "-r", "--name-only", REF)
            self._names = sorted({Path(p).stem for p in out.splitlines() if p.endswith(".jsonl")
                                  and mf.NAME.match(Path(p).stem) and rel_path(Path(p).stem) == p})
        return self._names

    def search(self, query: str) -> list[Line]:
        """The newest version that is not yanked of each package whose name (or description) holds `query`."""
        q = (query or "").lower()
        out = []
        for n in self.names():
            if q in n or not q:
                vs = [v for v in self.versions(n) if not v.yanked] or self.versions(n)
                if vs:
                    out.append(vs[0])
            else:
                vs = self.versions(n)
                if vs and vs[0].description and q in vs[0].description.lower():
                    out.append(next((v for v in vs if not v.yanked), vs[0]))
        return out


# ------------------------------------------------------------------ the choice of versions


@dataclass
class Want:
    """A requirement on an index package: local name, package, requirement, index URL, and the chain that asks for
    it ([project, 'analog_ip 1.2.0', ...] as text)."""
    local: str
    package: str
    req: Req
    index: str
    chain: tuple[str, ...]

    def text(self) -> str:
        return " -> ".join([*self.chain, f"{self.local} {self.req}" if self.local == self.package
                            else f"{self.local} ({self.package} {self.req})"])


@dataclass
class Unsolved:
    code: str
    message: str
    chains: list[str] = field(default_factory=list)


class SolveError(Exception):
    def __init__(self, problems: list[Unsolved]):
        self.problems = problems
        super().__init__("; ".join(p.message for p in problems))


Lines = Callable[[str, str], list[Line]]


def solve(wants: list[Want], lines: Lines, *, held: dict[tuple[str, str], Version] | None = None,
          prefer: dict[tuple[str, str], Version] | None = None, locked: set[tuple[str, str, Version]] | None = None,
          pdk_check: Callable[[Line], str | None] | None = None) -> dict[tuple[str, str], Line]:
    """The version of each index package the requirements `wants` (and, through the index lines, their own
    dependencies) reach: {(index URL, package): Line}. lines(url, package): its versions, newest first. held: packages
    that must stay at their locked version (an operation about another dependency). prefer: locked versions tried
    first, so a resolution moves nothing it does not have to. locked: (url, package, version) the lock has, which may
    be yanked. pdk_check(line): why a version does not fit the project's PDK, or None. Raises SolveError naming the
    requirements that cannot all be met."""
    held, prefer, locked = held or {}, prefer or {}, locked or set()
    failures: list[tuple[int, Unsolved]] = []
    steps = [0]

    def candidates(key: tuple[str, str], reqs: list[Want]) -> list[Line]:
        url, name = key
        out = []
        for line in lines(url, name):
            if line.yanked and (url, name, line.vers) not in locked:
                continue
            if key in held and line.vers != held[key]:
                continue
            if all(w.req.matches(line.vers) for w in reqs):
                out.append(line)
        if key in prefer:
            out.sort(key=lambda x: x.vers != prefer[key])            # the locked one first, then newest first
        return out

    def fail(depth: int, u: Unsolved) -> None:
        failures.append((depth, u))

    def explain(key: tuple[str, str], reqs: list[Want], depth: int) -> None:
        url, name = key
        every = lines(url, name)
        asks = " and ".join(f"{w.text()}" for w in reqs)
        if not every:
            fail(depth, Unsolved("no_package", f"{asks}: index {url} lists no package {name}"
                                 " (`pkg index update` fetches it again, `pkg search` looks for a name)",
                                 [w.text() for w in reqs]))
            return
        if key in held and not any(line.vers == held[key] for line in every):
            fail(depth, Unsolved("held", f"{asks}: {name} is locked at {held[key]}, which {url} no longer lists",
                                 [w.text() for w in reqs]))
            return
        match = [x for x in every if all(w.req.matches(x.vers) for w in reqs)]
        if key in held:
            if all(w.req.matches(held[key]) for w in reqs):
                return
            fail(depth, Unsolved("held", f"{asks}: {name} is locked at {held[key]}: `pkg update {name}` moves it",
                                 [w.text() for w in reqs]))
            return
        if not match:
            listed = ", ".join(str(x.vers) + (" (yanked)" if x.yanked else "") for x in every[:12])
            code = "conflict" if len(reqs) > 1 and all(any(w.req.matches(x.vers) for x in every) for w in reqs) \
                else "no_version"
            why = "no version meets all of them" if code == "conflict" else "no version matches"
            fail(depth, Unsolved(code, f"{name}: {why}: {asks}; {url} lists {listed}"
                                 + (" (one version per package)" if code == "conflict" else ""),
                                 [w.text() for w in reqs]))
            return
        live = [x for x in match if not x.yanked or (url, name, x.vers) in locked]
        if not live:
            fail(depth, Unsolved("yanked", f"{asks}: every {name} version that matches is yanked "
                                 f"({', '.join(str(x.vers) for x in match)})", [w.text() for w in reqs]))
            return
        if pdk_check is not None:
            why = [f"{x.vers}: {pdk_check(x)}" for x in live if pdk_check(x)]
            if len(why) == len(live):
                fail(depth, Unsolved("pdk", f"{asks}: no {name} version that matches fits this project's PDK ("
                                     + "; ".join(why[:4]) + ")", [w.text() for w in reqs]))

    def search(assign: dict, reqs: dict, pending: list[Want], depth: int) -> dict | None:
        steps[0] += 1
        if steps[0] > _STEPS:
            raise SolveError([Unsolved("too_complex", f"the package requirements did not resolve within {_STEPS} "
                                       "steps: pin some of them (=x.y.z) and try again")])
        if not pending:
            return assign
        w, rest = pending[0], pending[1:]
        key = (w.index, w.package)
        other = next((k for k in assign if k[1] == w.package and k[0] != w.index), None)
        if other is not None:
            fail(depth, Unsolved("two_indexes", f"{w.package}: from two indexes, {other[0]} ({' and '.join(x.text() for x in reqs[other])}) "
                                 f"and {w.index} ({w.text()}): one index per package name",
                                 [*(x.text() for x in reqs[other]), w.text()]))
            return None
        now = [*reqs.get(key, []), w]
        if key in assign:
            if w.req.matches(assign[key].vers):
                return search(assign, {**reqs, key: now}, rest, depth + 1)
            explain(key, now, depth)
            return None
        cands = candidates(key, now)
        if pdk_check is not None:
            cands = [c for c in cands if not pdk_check(c)]
        if not cands:
            explain(key, now, depth)
            return None
        for line in cands:
            more = [Want(d.local, d.package, d.req, d.index or w.index, (*w.chain, line.label)) for d in line.deps]
            got = search({**assign, key: line}, {**reqs, key: now}, rest + more, depth + 1)
            if got is not None:
                return got
        if not any(d >= depth for d, _ in failures):
            explain(key, now, depth)
        return None

    got = search({}, {}, list(wants), 0)
    if got is None:
        deepest = max(d for d, _ in failures) if failures else 0
        seen, out = set(), []
        for d, u in sorted(failures, key=lambda x: -x[0]):
            if d == deepest and u.message not in seen:
                seen.add(u.message)
                out.append(u)
        raise SolveError(out or [Unsolved("unsolved", "the package requirements cannot all be met")])
    return got
