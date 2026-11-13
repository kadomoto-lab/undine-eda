"""Transitive resolution of library dependencies (docs/design/packages.md, Resolution 1-6, Lockfile, Decisions 3-5).

Pure: no git, no network, no writes. The deps layer (store/deps.py) gives the project's requirements and a reader that
turns one requirement into a Node (its manifest read from git, the store or the install); this module walks the
graph, checks it and returns a Resolution, or raises ResolveError with every problem found.

A resolved version of a package is (package name, tree). There is no index yet, so every requirement is a git URL and
has exactly one candidate: the walk never chooses, it only checks that the requirements agree.

- One version per package name (Decisions 3). A node's identity is its manifest's [package] name, or `git:<local>`
  for a tree with no manifest (only project.json may name one). Two nodes of one name agree when their trees are
  equal, or when their `files` modes differ (a phase 1 entry hashed over every file, a phase 2 one over the include
  set) and they are the same (git, commit, subdir); then the locked node stands.
- Local names are project-wide (Decisions 5): each maps to one package, compared casefolded because one directory of
  a case-insensitive file system serves both spellings. A package with a manifest is also installed under its own
  [package] name.
- Cell names are disjoint (Decisions 4): check_disjoint, run by the deps layer through `cells`.

lock_graph, reachable and incomplete read the same graph back from a format 2 lock (Lockfile): direct dependencies in
`libraries`, the packages reached only through manifests in `packages`, each with its local `names` and its `deps`
({local name: package}).
"""
from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Literal

from undine.store import manifest as mf
from undine.store.semver import Req, Version

_CHAINS = 8                         # chains named per package: a dense graph has many, the first few say enough
_LOCK = "undine.lock"


@dataclass
class Requirement:
    """One dependency as written: in project.json (requirer None) or in the manifest of node `requirer` (an id).
    package None: any package (a plain project.json dependency). commit: read exactly this commit instead of
    resolving rev; set for a sibling, which is the requirer's repository at the requirer's commit."""
    local: str
    package: str | None = None
    git: str | None = None
    rev: str | None = None
    subdir: str | None = None
    version: Req | None = None
    requirer: str | None = None
    commit: str | None = None


@dataclass(eq=False)
class Node:
    """A resolved tree. fixed: a locked entry the deps layer did not re-resolve (built by store/locked.py); its
    chain_hint is how the lock reached it, for messages about it before the walk does. held: a fixed entry locked
    before phase 2 (no package recorded), taken as the plain entry it was: its manifest gives its identity, but
    its dependencies, its own name and its [pdk] wait for lib lock or lib update of it. files may stay empty until
    the deps layer reads the tree (cells)."""
    id: str
    package: str | None = None
    version: Version | None = None
    git: str | None = None
    rev: str | None = None
    subdir: str | None = None
    commit: str | None = None
    tree: str | None = None
    files_mode: Literal["all", "include"] = "all"
    files: dict = field(default_factory=dict)
    manifest: mf.Manifest | None = None
    fixed: bool = False
    chain_hint: str | None = None
    held: bool = False


Reader = Callable[[Requirement, Node | None], Node]
"""Reads the tree a requirement names; the second argument is the requirer's node (None for project.json). It raises
ManifestError for a bad manifest (collected as a problem) and anything else (git failing) to stop the walk."""

CODES = ("conflict", "local_name", "reserved_name", "package_mismatch", "no_manifest", "index_dependency",
         "local_url", "pdk", "cell_clash", "version", "manifest")


@dataclass
class Problem:
    code: str
    message: str
    chains: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.message


class ResolveError(Exception):
    """Every problem of one resolution; the message lists them and ends with 'nothing was changed'."""

    def __init__(self, problems: list[Problem]):
        self.problems = list(problems)
        if len(self.problems) == 1:
            text = f"{self.problems[0].message}; nothing was changed"
        else:
            text = (f"{len(self.problems)} problems with the dependencies:\n"
                    + "\n".join(f"- {p.message}" for p in self.problems) + "\nnothing was changed")
        super().__init__(text)


@dataclass
class Resolution:
    """nodes: {id: Node}. names: {local name: id}, every name a package is installed under (direct, transitive and
    own names). deps: {id: {local name: id}} per node. direct: {project.json name: id}. chains: {id: [[root, id, ...,
    id], ...]}, the ways the project reaches each node."""
    nodes: dict[str, Node] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    deps: dict[str, dict[str, str]] = field(default_factory=dict)
    direct: dict[str, str] = field(default_factory=dict)
    chains: dict[str, list[list[str]]] = field(default_factory=dict)

    def names_of(self, id: str) -> list[str]:
        """The local names node `id` is installed under, in the order the walk met them."""
        return [n for n, i in self.names.items() if i == id]

    def requirers(self, id: str) -> list[str]:
        """The nodes whose manifests name node `id` (its `via`)."""
        return [r for r, d in self.deps.items() if id in d.values() and r != id]

    def chain_texts(self, id: str) -> list[str]:
        return [chain_text([c[0], *(self.nodes[i] for i in c[1:])]) for c in self.chains.get(id, [])]


# ------------------------------------------------------------------ naming


def is_local_url(url) -> bool:
    """A repository on this machine (file://, an absolute path, ~/): only a requirer that is itself local may name
    one, or a remote package could read a private repository into the project."""
    return isinstance(url, str) and url.startswith(("file:", "/", "~"))


def norm_git(url) -> str:
    """A git URL compared loosely: no trailing / or .git, the host in lower case."""
    u = (url or "").strip().rstrip("/")
    u = u[:-4] if u.endswith(".git") else u
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/]*)(.*)$", u)
    return f"{m[1].lower()}{m[2].lower()}{m[3]}" if m else u


def _c12(commit) -> str:
    return (commit or "")[:12]


def name_text(node: Node) -> str:
    """'analog_ip 1.2.0', or '<local> (git <commit12>)' for a tree without a manifest (overview rule d)."""
    if node.package:
        return f"{node.package} {node.version}"
    local = node.id[4:] if node.id.startswith("git:") else node.id
    return f"{local} (git {_c12(node.commit)})"


def node_text(node: Node) -> str:
    """'analog_ip 1.2.0 (v1.2.0 @ 229914698b1f)': name, version, rev and commit, as a chain shows it."""
    if not node.package:
        return name_text(node)
    at = f"{node.rev} @ {_c12(node.commit)}" if node.rev else _c12(node.commit)
    return f"{node.package} {node.version} ({at})"


def chain_text(nodes) -> str:
    """'app -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f) -> sar_common 1.3.1 (v1.3.1 @ 5b1c0e2a9d11)'. Items are Nodes or
    text (the project's name first)."""
    return " -> ".join(x if isinstance(x, str) else node_text(x) for x in nodes)


def _req_text(req: Requirement) -> str:
    """A requirement not (or not successfully) read: 'bias (https://... v0.3.2 subdir lib)'."""
    parts = [x for x in (req.git, req.rev, f"subdir {req.subdir}" if req.subdir else None) if x]
    return f"{req.local} ({' '.join(parts)})" if parts else req.local


def _place(git, rev, subdir) -> tuple:
    return norm_git(git), rev or None, subdir or None


def _where(node: Node) -> str:
    sub = f" subdir {node.subdir}" if node.subdir else ""
    rev = f"{node.rev} " if node.rev else ""
    return f"{node.git} {rev}@ {_c12(node.commit)}{sub}"


# ------------------------------------------------------------------ identity


def same_package(a: Node, b: Node) -> bool:
    """Whether two nodes of one package name are one version (overview rule h): equal trees; or, when one is hashed
    over every file and the other over the include set, the same (normalised git, commit, subdir). Without both
    trees (a reader that has not hashed the files yet), the same (git, commit, subdir) is the same content."""
    if a.tree and b.tree and a.tree == b.tree:
        return True
    same_place = (norm_git(a.git), a.commit, a.subdir or None) == (norm_git(b.git), b.commit, b.subdir or None)
    if a.files_mode != b.files_mode or not (a.tree and b.tree):
        return bool(a.commit) and same_place
    return False


# ------------------------------------------------------------------ the walk


def resolve(root: str, direct: dict[str, Requirement], read: Reader, *, fixed: dict[str, Node] | None = None,
            reserved: dict[str, str] | None = None, pdk_check=None, cells=None) -> Resolution:
    """Resolves `direct` (project.json's git dependencies, by local name) and everything their manifests name,
    breadth first. `fixed`: locked nodes not re-resolved, by id; a requirement naming one's git, rev and subdir takes
    it without reading. `reserved`: {casefolded name: what it is} that no local name may be. pdk_check(node): why a
    node with a [pdk] does not fit the project's PDK, or None. cells(resolution): the cell-name problems, run last.
    Raises ResolveError once, with every problem, after every reachable manifest has been read."""
    return _Walk(root, read, fixed or {}, reserved or {}).run(direct, pdk_check, cells)


class _Walk:
    def __init__(self, root: str, read: Reader, fixed: dict[str, Node], reserved: dict[str, str]):
        self.root, self.read, self.fixed, self.reserved = root, read, fixed, reserved
        self.res = Resolution()
        self.problems: list[Problem] = []
        self.queue: deque[Requirement] = deque()
        self.cache: dict[tuple, Node | None] = {}
        self.first: dict[str, str | None] = {}          # id -> the requirer it was first reached from (parent pointer)
        self.parents: dict[str, list[str | None]] = {}
        self.seen: dict[str, tuple[str, str, list]] = {}    # casefolded local name -> (spelling, id, chain)
        self.told: set[tuple] = set()

    def problem(self, code: str, message: str, chains: list[str] | None = None) -> None:
        self.problems.append(Problem(code, message, chains or []))

    # -- chains

    def chain(self, id: str | None) -> list:
        """[root, node, ..., node] along parent pointers to node `id` (root alone for None)."""
        out: list = []
        seen = set()
        while id is not None and id not in seen:
            seen.add(id)
            out.append(self.res.nodes[id])
            id = self.first.get(id)
        return [self.root, *reversed(out)]

    def all_chains(self, id: str) -> list[list[str]]:
        def walk(i: str, on: frozenset) -> list[list[str]]:
            out: list[list[str]] = []
            for p in self.parents.get(i, []):
                if p is None:
                    out.append([i])
                elif p not in on:
                    out += [c + [i] for c in walk(p, on | {i})]
                if len(out) >= _CHAINS:
                    break
            return out[:_CHAINS]
        return [[self.root, *c] for c in walk(id, frozenset({id}))]

    # -- run

    def run(self, direct: dict[str, Requirement], pdk_check, cells) -> Resolution:
        for local, req in direct.items():
            req.local, req.requirer = local, None
            self.queue.append(req)
        while self.queue:
            self.step(self.queue.popleft())
        res = self.res
        res.chains = {id: self.all_chains(id) for id in res.nodes}
        if pdk_check is not None:
            for node in res.nodes.values():
                if node.manifest is not None and node.manifest.pdk is not None and not node.held:
                    reason = pdk_check(node)
                    if reason:
                        chains = res.chain_texts(node.id)
                        self.problem("pdk", f"{name_text(node)} does not fit this project's PDK: {reason} (required "
                                            f"by {' and by '.join(chains)})", chains)
        if cells is not None:
            self.problems += list(cells(res) or [])
        if self.problems:
            raise ResolveError(self.problems)
        return res

    def step(self, req: Requirement) -> None:
        parent = self.res.nodes.get(req.requirer) if req.requirer is not None else None
        if parent is not None and is_local_url(req.git) and not is_local_url(parent.git):
            text = chain_text([*self.chain(parent.id), _req_text(req)])
            self.problem("local_url", f"{text}: {req.git} is a path on this machine, and {name_text(parent)} comes "
                                      f"from {parent.git}: a package from elsewhere may not read local repositories; "
                                      "ask its publisher for a git URL", [text])
            return
        if parent is not None and req.package and any(
                not isinstance(n, str) and n.id == req.package for n in self.chain(parent.id)):
            self.link(req, self.res.nodes[req.package])          # a cycle: that package is resolved already
            self.check_version(req, self.res.nodes[req.package])
            return
        node = self.take(req, parent)
        if node is None or not self.check(req, node):
            return
        have = self.res.nodes.get(node.id)
        if have is None:
            self.res.nodes[node.id] = node
            self.first[node.id] = req.requirer
            self.res.deps.setdefault(node.id, {})
            self.link(req, node)                                 # the requirer's name first, then the node's own
            self.expand(node)
            return
        if have is not node:
            if not same_package(have, node):
                self.conflict(req, have, node)
                return
            if node.fixed and not have.fixed:                    # the locked entry stands
                self.res.nodes[node.id] = node
        self.link(req, self.res.nodes[node.id])

    def take(self, req: Requirement, parent: Node | None) -> Node | None:
        """The node for a requirement: a fixed one at the same git, rev and subdir, else read once per place."""
        place = _place(req.git, req.rev, req.subdir)
        for node in self.fixed.values():
            if (_place(node.git, node.rev, node.subdir) == place and (req.commit is None or node.commit == req.commit)
                    and (node.manifest is not None or node.id == f"git:{req.local}")):
                self.identify(node, req)
                return node
        key = (req.git, req.rev, req.subdir or None, req.commit)
        if key not in self.cache:
            try:
                node = self.read(req, parent)
            except mf.ManifestError as e:
                text = chain_text([*self.chain(req.requirer), _req_text(req)])
                self.problem("manifest", f"{e} (required by {text})", [text])
                node = None
            if node is not None:
                self.identify(node, req)
            self.cache[key] = node
        node = self.cache[key]
        if node is not None and node.manifest is None and node.id != f"git:{req.local}":
            node = replace(node, id=f"git:{req.local}")         # one plain tree under two names: two nodes
        return node

    @staticmethod
    def identify(node: Node, req: Requirement) -> None:
        """A node's identity comes from its manifest, whatever the reader set: the [package] name, else git:<local>."""
        if node.manifest is not None:
            node.id, node.package, node.version = node.manifest.name, node.manifest.name, node.manifest.version
        else:
            node.id, node.package, node.version = f"git:{req.local}", None, None

    def check(self, req: Requirement, node: Node) -> bool:
        """Rules d and e. False: the node is not what the requirement means, and is not taken."""
        text = chain_text([*self.chain(req.requirer), node])
        if node.manifest is None:
            if req.requirer is not None:
                self.problem("no_manifest", f"{chain_text(self.chain(req.requirer))} -> {req.local}: {_where(node)} "
                                            f"has no {mf.MANIFEST}: a package may depend only on packages (only "
                                            "project.json names a plain git library)", [text])
                return False
            if req.package:
                self.problem("package_mismatch", f"{req.local}: project.json asks for package {req.package}, but "
                                                 f"{_where(node)} has no {mf.MANIFEST}: drop package, or fix git, rev "
                                                 "and subdir", [text])
                return False
            return True
        if req.package and req.package != node.package:
            self.problem("package_mismatch", f"{text}: {req.local} asks for package {req.package}, but "
                                             f"{_where(node)} is {node.package}: fix package, or git, rev and subdir "
                                             f"of {req.local}", [text])
            return False
        self.check_version(req, node)
        return True

    def check_version(self, req: Requirement, node: Node) -> None:
        if req.version is not None and node.version is not None and not req.version.matches(node.version):
            text = chain_text([*self.chain(req.requirer), node])
            self.problem("version", f"{text}: {req.local} requires {node.package} {req.version}, but "
                                    f"{node.rev or _c12(node.commit)} is {node.version}: point rev at a version "
                                    "that matches, or change version", [text])

    def conflict(self, req: Requirement, have: Node, node: Node) -> None:
        old = chain_text(self.chain(have.id)) if have.id in self.first else (have.chain_hint or node_text(have))
        new = chain_text([*self.chain(req.requirer), node])
        self.problem("conflict", f"{node.package or node.id}: two versions are required, {old} and {new}: align the "
                                 "revs (one version per package)", [old, new])

    def expand(self, node: Node) -> None:
        """A node taken for the first time: its own name, and its manifest's dependencies queued."""
        m = node.manifest
        if m is None or node.held:                               # held: as locked before phase 2, nothing more
            return
        self.name(m.name, node, None, own=True)
        for local, dep in m.dependencies.items():
            if isinstance(dep, mf.IndexDep):
                self.index_dependency(node, dep)
                continue
            sibling = dep.git is None
            self.queue.append(Requirement(
                local, dep.package, node.git if sibling else dep.git, node.rev if sibling else dep.rev, dep.subdir,
                dep.version, node.id, node.commit if sibling else None))

    def index_dependency(self, node: Node, dep: mf.IndexDep) -> None:
        keys = ['git = "https://..."', 'rev = "v1.2.0"']
        if dep.package != dep.local:
            keys.insert(0, f'package = "{dep.package}"')
        if dep.version != "*":
            keys.append(f'version = "{dep.version}"')
        text = chain_text(self.chain(node.id))
        index = f", index {dep.index}" if dep.index else ""
        self.problem("index_dependency",
                     f"{text}: {mf.MANIFEST}: dependencies.{dep.local}: an index requirement ({dep.package} "
                     f"{dep.version}{index}), which comes with the package index (phase 3): name its git repository, "
                     f"{dep.local} = {{ {', '.join(keys)} }}", [text])

    def link(self, req: Requirement, node: Node) -> None:
        if req.requirer is None:
            self.res.direct[req.local] = node.id
        else:
            self.res.deps.setdefault(req.requirer, {})[req.local] = node.id
        ps = self.parents.setdefault(node.id, [])
        if req.requirer not in ps:
            ps.append(req.requirer)
        self.name(req.local, node, req.requirer)

    def name(self, local: str, node: Node, requirer: str | None, own: bool = False) -> None:
        """Rules b, c and f: one package per local name (casefolded), none of them reserved."""
        chain = self.chain(node.id) if own else [*self.chain(requirer), node]
        text = chain_text(chain) + (" (its own name)" if own else "")
        cf = local.casefold()
        if cf in self.reserved:
            if (cf, node.id) not in self.told:
                self.told.add((cf, node.id))
                self.problem("reserved_name", f"{local}: {text} installs it under this local name, which is "
                                              f"{self.reserved[cf]}: a dependency may not shadow it; rename it in the "
                                              "requirer's [dependencies]", [text])
            return
        seen = self.seen.get(cf)
        if seen is None:
            self.seen[cf] = (local, node.id, text)
        elif seen[1] == node.id and seen[0] != local:
            # one package, but one directory of a case-insensitive file system would serve both spellings, and
            # removing either would take the other's install
            if (cf, local, seen[0]) not in self.told:
                self.told.add((cf, local, seen[0]))
                self.problem("local_name", f"{seen[0]} and {local}: one local name spelled two ways, {seen[2]} and "
                                           f"{text}: spell it one way", [seen[2], text])
            return
        elif seen[1] != node.id:
            if (cf, seen[1], node.id) not in self.told:
                self.told.add((cf, seen[1], node.id))
                names = local if seen[0] == local else f"{seen[0]} and {local}"
                self.problem("local_name", f"{names}: one local name for two packages, {seen[2]} and {text}: give one "
                                           "of them another local name in its requirer's [dependencies]",
                             [seen[2], text])
            return
        self.res.names.setdefault(local, node.id)


# ------------------------------------------------------------------ cells


def cell_names(paths) -> tuple[dict[str, str], dict[str, str]]:
    """The cells of a library's files (POSIX paths below its directory): (logic, layout_only), each {casefolded name:
    name}. Logic cells are the top-level stems of .sch, .sym, .sv and .v; layout-only cells are top-level .oas cells
    with no logic view (copies of placed cells, subcells). Generated device cells and files in subdirectories are
    not cells of the library."""
    from undine.layouts import layoutstore
    from undine.store.project import DEVICE_CELL
    logic: dict[str, str] = {}
    layout: dict[str, str] = {}
    for p in paths:
        if "/" in p or p.startswith("."):
            continue
        path = PurePosixPath(p)
        if path.suffix in (".sch", ".sym", ".sv", ".v"):
            name, into = path.stem, logic
        elif path.suffix == layoutstore.SUFFIX and p != layoutstore.LEGACY:
            name, into = layoutstore.cell_name(path), layout
        else:
            continue
        if not DEVICE_CELL.match(name):
            into.setdefault(name.casefold(), name)
    return logic, {cf: n for cf, n in layout.items() if cf not in logic}


@dataclass
class _Owner:
    text: str
    kind: str                         # 'package', 'project' or 'pdk'
    manifested: bool = False
    closure: frozenset = frozenset()


def check_disjoint(packages: dict, project: dict, pdk: dict) -> list[Problem]:
    """Cell-name clashes (Resolution 4) that refuse a resolution. packages: {label: (logic, layout_only,
    closure_labels, has_manifest)}, logic and layout_only as cell_names gives them, keyed by package (one package
    under two local names is one entry). project: {library: logic cell names}; pdk: {library: cell names}.

    A clash needs a package with a manifest on one side: two plain dependencies, or the project and the PDK, are
    never refused here (status notes them). Logic cells clash with logic cells of another package, of the project
    or of the PDK. A package's layout-only cell clashes only with the logic cell of a package outside its dependency
    closure: a copy of a cell it places, or of a PDK cell, is no clash. The project's layout-only cells (copies
    placed from packages, subcells) are not given at all."""
    views: dict[str, list[tuple[_Owner, str, bool]]] = {}       # cf -> [(owner, name, logic?)]

    def add(owner: _Owner, names, logic: bool) -> None:
        items = names.items() if isinstance(names, dict) else ((n.casefold(), n) for n in names)
        for cf, n in items:
            views.setdefault(cf, []).append((owner, n, logic))

    for label, (logic, layout, closure, manifested) in packages.items():
        owner = _Owner(label, "package", bool(manifested), frozenset(closure or ()))
        add(owner, logic, True)
        add(owner, layout, False)
    for lib, names in project.items():
        add(_Owner(f"project library {lib}", "project"), names, True)
    for lib, names in pdk.items():
        add(_Owner(f"PDK library {lib}", "pdk"), names, True)

    out: list[Problem] = []
    for entries in views.values():
        for i, (a, na, la) in enumerate(entries):
            for b, nb, lb in entries[i + 1:]:
                if _clash(a, la, b, lb):
                    name = na if na == nb else f"{na} and {nb}"
                    out.append(Problem("cell_clash", f"{name}: {a.text} and {b.text}"))
    return out


def _clash(a: _Owner, a_logic: bool, b: _Owner, b_logic: bool) -> bool:
    if a is b or a.text == b.text or not (a.manifested or b.manifested):
        return False                          # one package, or no package with a manifest (project, PDK, plain)
    if a_logic and b_logic:
        return True
    if not (a_logic or b_logic):
        return False                          # two layout copies
    lay, logic = (a, b) if not a_logic else (b, a)
    return lay.kind == "package" and logic.kind == "package" and logic.text not in lay.closure


# ------------------------------------------------------------------ the lock's graph


@dataclass
class Graph:
    """A lock's packages as a graph (Lockfile). entries: {id: entry}, id the entry's package, or git:<key> for a
    libraries entry without one. keys: {id: (table, key)}. direct: {libraries key: id}. deps: {id: {local: id}}
    for the targets the lock has; missing: {id: {local: package}} for those it does not. twice: package names
    locked in more than one entry."""
    entries: dict[str, dict] = field(default_factory=dict)
    keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    direct: dict[str, str] = field(default_factory=dict)
    deps: dict[str, dict[str, str]] = field(default_factory=dict)
    missing: dict[str, dict[str, str]] = field(default_factory=dict)
    twice: list[str] = field(default_factory=list)

    def names_of(self, id: str) -> list[str]:
        """The local names an entry is installed under: its `names` (strings only), else its key; a libraries
        entry's key always first."""
        table, key = self.keys[id]
        names = [n for n in self.entries[id].get("names") or [] if isinstance(n, str)]
        if table == "libraries":
            return [key, *(n for n in names if n != key)]
        return names or [key]

    def label(self, id: str) -> str:
        """'analog_ip 1.2.0 (v1.2.0 @ 229914698b1f)', or '<key> (git <commit12>)' without a package."""
        e = self.entries[id]
        commit = e.get("commit") if isinstance(e.get("commit"), str) else ""
        if not e.get("package"):
            return f"{self.keys[id][1]} (git {_c12(commit)})"
        at = f"{e['rev']} @ {_c12(commit)}" if e.get("rev") else _c12(commit)
        return f"{e['package']} {e.get('version')} ({at})"

    def requirers(self, id: str) -> list[str]:
        return [r for r, d in self.deps.items() if id in d.values() and r != id]

    def chains(self, id: str, limit: int = _CHAINS) -> list[list[str]]:
        """The ways from a direct entry to `id`: [[id of a direct entry, ..., id], ...]."""
        def walk(i: str, on: frozenset) -> list[list[str]]:
            out = [[i]] if i in self.direct.values() else []
            for r in self.requirers(i):
                if r not in on:
                    out += [c + [i] for c in walk(r, on | {i})]
                if len(out) >= limit:
                    break
            return out[:limit]
        return walk(id, frozenset({id}))

    def chain_text(self, root: str, chain: list[str]) -> str:
        return " -> ".join([root, *(self.label(i) for i in chain)])


def _str(x) -> str | None:
    return x if isinstance(x, str) and x else None


def lock_graph(lock: dict | None) -> Graph:
    """The graph of a lock's `libraries` and `packages` (format 1 and phase 1 locks: direct entries only). Entries
    that are not objects are left out, as sync leaves them (bad_lock_keys reports their keys)."""
    g = Graph()
    lock = lock or {}
    libraries = lock.get("libraries") if isinstance(lock.get("libraries"), dict) else {}
    packages = lock.get("packages") if isinstance(lock.get("packages"), dict) else {}
    found: dict[str, str] = {}                   # package name -> id of the entry deps targets resolve to
    for key, e in packages.items():
        if isinstance(e, dict) and isinstance(key, str):
            g.entries[key], g.keys[key] = e, ("packages", key)
            found[key] = key
    for key, e in libraries.items():
        if not (isinstance(e, dict) and isinstance(key, str)):
            continue
        package = _str(e.get("package"))
        id = package if package and package not in g.entries else f"git:{key}"
        if package and package in g.entries:
            g.twice.append(package)
        g.entries[id], g.keys[id], g.direct[key] = e, ("libraries", key), id
        if package:
            found.setdefault(package, id)
    for id, e in g.entries.items():
        deps = e.get("deps") if isinstance(e.get("deps"), dict) else {}
        for local, package in deps.items():
            target = found.get(package) if isinstance(package, str) else None
            if target is None:
                g.missing.setdefault(id, {})[str(local)] = str(package)
            else:
                g.deps.setdefault(id, {})[str(local)] = target
    return g


def reachable(lock: dict | None, direct_names) -> tuple[set[str], set[str]]:
    """(ids, local names) of the entries reached from the direct dependencies `direct_names` (project.json's git
    names) through `deps`, and the names they are installed under."""
    g = lock_graph(lock)
    todo = deque(g.direct[n] for n in direct_names if n in g.direct)
    ids: set[str] = set()
    while todo:
        id = todo.popleft()
        if id not in ids:
            ids.add(id)
            todo.extend(g.deps.get(id, {}).values())
    return ids, {n for id in ids for n in g.names_of(id)}


def incomplete(lock: dict | None, reserved: dict[str, str] | None = None, manifests: dict | None = None) -> list[str]:
    """Why a format 2 lock's graph cannot be installed as it is, one reason per problem ('<who>: <what>'), [] when it
    can. reserved: {casefolded name: what it is}, as resolve takes it. manifests: {libraries key: Manifest | None}
    of entries without `deps`, when the caller has them: a phase 1 entry whose manifest names dependencies is not
    complete until lib lock or lib update resolves it."""
    from undine.store.project import NAME_RE
    g = lock_graph(lock)
    reserved = reserved or {}
    out: list[str] = []
    for package in g.twice:
        out.append(f"{package}: locked twice, in packages and in libraries")
    for id, (table, key) in g.keys.items():
        if table == "packages" and g.entries[id].get("package") != key:
            out.append(f"packages.{key}: says package {g.entries[id].get('package')!r}")
    for id, missing in g.missing.items():
        for local, package in missing.items():
            out.append(f"{g.label(id)}: needs {package} (as {local}), which {_LOCK} does not lock")
    plain = {k.casefold(): k for k, id in g.direct.items() if not g.entries[id].get("package")}
    claimed: dict[str, tuple[str, str]] = {}            # casefolded name -> (spelling, id)
    for id, e in g.entries.items():
        table, key = g.keys[id]
        if "deps" in e and not e.get("names") and table == "libraries":
            out.append(f"{key}: has deps but no names")
        for n in g.names_of(id):
            cf = n.casefold()
            if not NAME_RE.match(n):
                out.append(f"{key}: {n!r} is not a library name")
                continue
            if cf in reserved:
                out.append(f"{key}: the local name {n} is {reserved[cf]}")
            if cf in plain and plain[cf] != key and e.get("package"):
                out.append(f"{key}: the local name {n} is the plain git dependency {plain[cf]}")
                continue
            have = claimed.setdefault(cf, (n, id))
            if have[1] != id:
                out.append(f"{n}: one local name for {g.label(have[1])} and {g.label(id)}")
    for key, m in (manifests or {}).items():
        id = g.direct.get(key)
        if id is not None and m is not None and m.dependencies and "deps" not in g.entries[id]:
            out.append(f"{key}: its {mf.MANIFEST} names dependencies ({', '.join(m.dependencies)}) that {_LOCK} does "
                       "not lock")
    return out
