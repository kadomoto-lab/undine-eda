"""The check report, undine-check.json (docs/design/packages.md, Publishing): what `pkg check` found for one package
version, bound to the commit and tree it ran on. `pkg publish` links it from the index line by URL and SHA-256, and
the index CI verifies it there.

    build(src, ...)              the report: the checks that need no PDK (static) and, until the runs exist, the
                                 runs marked not_run
    static(src, tag, commit)     licence, file sizes, LFS, dependencies (no git ones; solved from the index), the PDK
                                 commit, the local names the schematics place, internal cells placed, the code blocks
                                 and RTL listed with what the trust rule would refuse
    write(path, report)          the bytes written, and their SHA-256
    verify(data, line, sha256)   the problems publish and the index CI share: another SHA-256, another package,
                                 version, commit or tree than the line's, a format this Undine does not know, a report
                                 that does not pass

Format 1, every path relative to the package (the report is published: no absolute path, no user name, no URL
user or password; build() replaces them, see _path_subs and public_url):

    {"format": 1, "kind": "undine-check", "created": "2026-11-22T10:00:00Z",
     "package": {name, version, repository, subdir, tag, commit, tree},
     "pdk": {family, variants, built_with, project},       project: the commit the checks ran on (the project's PDK)
     "tools": {...},                                        as the lock's tools (tools.tools_record)
     "publisher_project": {name, lib},                      where the runs' job ids live
     "files": {count, bytes, limit, largest: {path, bytes}, over: [{path, bytes}]},
     "license": {expression, ok, ids},
     "dependencies": [{name, package, req, index, resolved, problem}],
     "cells": {"<cell>": {exported, views: [...],
               symbol:  {state, passed, problems},
               netlist: {state, passed, warnings},
               drc:     {state, applies, deck, options, job, total, counted, waived: [{rule, count, reason}], passed},
               lvs:     {state, applies, why?, job, match, passed},
               specs:   {state, applies, passed, results: [{id, status, value, margin, runs}]}}},
     "runs": {"state": "done" | "not_run" | "incomplete", "why"},
     "semver": {state, previous, actual, required, changes: [{cell, kind, detail, needs}], specs_changed, passed},
     "code": {"blocks": [...], "rtl": [...]},               trust.listing
     "warnings": [{check, cell?, message}], "problems": [{check, cell?, message}], "passed": bool}

A check's `state` is "done", "not_run" or "error"; `passed` is true only when it ran and passed. The report passes
exactly when `problems` is empty. An exported cell whose spec is not met or not measured is a problem (decided
2026-11-22); a waiver applies to the waived cell's own DRC run only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from undine.store import cas, gitsrc
from undine.store import manifest as mf

FORMAT = 1
KIND = "undine-check"
FILE = "undine-check.json"
FILE_MAX = 50 * 1024 * 1024


def file_max() -> int:
    """The per-file size limit: UNDINE_PKG_FILE_MAX bytes, default 50 MB."""
    try:
        return int(os.environ.get("UNDINE_PKG_FILE_MAX", FILE_MAX))
    except ValueError:
        return FILE_MAX


def default_path(src) -> Path:
    """<project>/.undine/check/<name>-<version>/undine-check.json: outside the package's directory, so writing it
    does not change the tree it describes."""
    m = src.manifest
    return src.prj.path / ".undine" / "check" / f"{m.name}-{m.version}" / FILE


# ------------------------------------------------------------------ the static checks


class _Report:
    def __init__(self):
        self.warnings: list[dict] = []
        self.problems: list[dict] = []

    def problem(self, check: str, message: str, cell: str | None = None) -> None:
        self.problems.append({"check": check, **({"cell": cell} if cell else {}), "message": message})

    def warn(self, check: str, message: str, cell: str | None = None) -> None:
        self.warnings.append({"check": check, **({"cell": cell} if cell else {}), "message": message})


def _model_env(pdk) -> tuple[list[Path], dict]:
    """The roots a path may read and the variables the netlister substitutes for the project's PDK (as
    Server.model_env gives them), for the code listing; nothing without an installed PDK."""
    if pdk is None:
        return [], {}
    env = {"PDK_ROOT": str(pdk.root.parent), "PDK": pdk.name}
    for name, rel in getattr(pdk.profile, "model_vars", ()) or ():
        env[name] = str(pdk.root / rel)
    return [Path(pdk.root)], env


def _cells(files: dict) -> dict[str, list[str]]:
    """{cell: views} of the top-level cell files of a tree."""
    order = {".sch": "schematic", ".sym": "symbol", ".oas": "layout", ".sv": "rtl", ".v": "rtl"}
    out: dict[str, list[str]] = {}
    for p in files:
        if "/" in p:
            continue
        for suf, view in order.items():
            if p.endswith(suf):
                views = out.setdefault(p[: -len(suf)], [])
                if view not in views:
                    views.append(view)
    return {c: sorted(v, key=list(order.values()).index) for c, v in sorted(out.items())}


def _dependencies(r: _Report, src, index_url: str, offline: bool) -> list[dict]:
    from undine.pdks import compat
    from undine.pdks.pdk import family_of
    from undine.store import index as ix
    from undine.store.semver import Req, SemverError
    m = src.manifest
    rows: list[dict] = []
    wants = []
    for local, d in m.dependencies.items():
        if isinstance(d, mf.GitDep):
            rows.append({"name": local, "package": d.package, "req": str(d.version or "*"), "index": None,
                         "git": d.git, "resolved": None})
            r.problem("dependencies", f"{local} is a git dependency: a published package depends only on packages "
                                      "from an index: publish it to one and name it by version")
            continue
        try:
            url = ix.url_of(d.index, src.prj.meta) if d.index else index_url
            req = Req.parse(d.version)
        except (ix.IndexReadError, SemverError) as e:
            rows.append({"name": local, "package": d.package, "req": d.version, "index": d.index, "resolved": None})
            r.problem("dependencies", f"{local}: {e}")
            continue
        rows.append({"name": local, "package": d.package, "req": d.version, "index": url, "resolved": None})
        wants.append(ix.Want(local, d.package, req, url, (f"{m.name} {m.version}",)))
    if not wants:
        return rows
    idx: dict[str, ix.Index] = {}
    unread: set[str] = set()
    for w in wants:
        if w.index in idx:
            continue
        idx[w.index] = ix.Index(w.index)
        try:
            if not offline:
                idx[w.index].update()
            elif not idx[w.index].fetched():
                unread.add(w.index)
        except ix.IndexReadError as e:
            unread.add(w.index)
            r.problem("dependencies", f"index {w.index} cannot be read: {e}")
    for u in sorted(unread):
        if offline:
            r.problem("dependencies", f"index {u} has no local copy, and --offline fetches none: run "
                                      "`python -m undine pkg index update` first")

    def lines(url: str, name: str):
        return [] if url in unread else idx.setdefault(url, ix.Index(url)).versions(name)
    target = None
    if m.pdk is not None and m.pdk.variants:
        v = m.pdk.variants[0]
        target = compat.Target(family_of(v), v, m.pdk.built_with)

    def pdk_check(line) -> str | None:
        req = line.pdk_req()
        return None if req is None or target is None else compat.check(req, target)
    try:
        got = ix.solve(wants, lines, pdk_check=pdk_check)
    except ix.SolveError as e:
        for p in e.problems:
            r.problem("dependencies", p.message)
        return rows
    except ix.IndexReadError as e:
        r.problem("dependencies", str(e))
        return rows
    for row in rows:
        line = got.get((row["index"], row["package"]))
        if line is not None:
            row["resolved"] = str(line.vers)
    return rows


def _local_names(r: _Report, src, tmp: Path, libs: dict) -> None:
    """Every library a schematic places must be the package itself, a declared dependency, a PDK library or
    `devices`; placing an internal cell of a dependency is a warning (docs/design/packages.md, phase 2 decisions)."""
    from undine.store import pkgsource
    m, own = src.manifest, src.lib.name
    pdk_libs = {n for n, x in libs.items() if x.origin == "pdk"} | {"devices"}
    pdk_here = src.prj.pdk is not None
    for local, cells in sorted(pkgsource.placements(tmp).items()):
        where = "; ".join(f"{c} in {', '.join(s)}" for c, s in sorted(cells.items()))[:300]
        if local == m.name or local in pdk_libs:
            continue
        if local == "":
            r.warn("local_names", f"symbols placed with no library part ({where}): they resolve through the "
                                  "symbol path, which another project may not have")
            continue
        if local == own:
            r.problem("local_names", f"the schematics place the library's own cells as {own}/... ({where}): a "
                                     f"consumer installs the package as {m.name}: place them as {m.name}/...")
            continue
        if local not in m.dependencies:
            r.problem("local_names", f"{local} is placed ({where}) but is neither the package, a declared dependency "
                                     "nor a PDK library: add it to [dependencies]"
                      + ("" if pdk_here else " (the project's PDK is not installed here, so its libraries are not "
                                             "known: install it if this is one of them)"))
            continue
        lib = libs.get(local)
        if lib is not None and lib.origin == "dep":
            inner = sorted(c for c in cells if lib.internal(c))
            if inner:
                r.warn("internal_cells", f"places internal cells of {local}: {', '.join(inner)} (not in its "
                                         "[exports]: a new version may change them without a major version)")


def static(r: _Report, src, tag: str, commit: str, *, index_url: str, offline: bool) -> dict:
    """The parts of the report that need no PDK. Writes into the store only (the tree, as a fetch does)."""
    from undine.store import pkgsource, spdx, trust
    m = src.manifest
    out: dict = {}
    # the repository serves the tag at this commit
    if not m.repository:
        r.problem("repository", "no package.repository: add the git URL consumers fetch from")
    elif offline:
        r.warn("repository", f"not checked (--offline) that {m.repository} serves {tag} at {commit[:12]}")
    else:
        try:
            got = pkgsource.served(m.repository, tag)
        except pkgsource.PackageSourceError as e:
            got = None
            r.problem("repository", str(e))
        else:
            if got != commit:
                r.problem("repository", f"{m.repository} does not serve {tag} at {commit[:12]}"
                          + (f" (it serves {got[:12]})" if got else " (no such tag there)")
                          + f": push the tag (`git push origin {tag}`)")
    # the tree, as a consumer reads it
    tree = None
    try:
        tree = pkgsource.tree(str(src.top), commit, src.subdir, tag)
    except (gitsrc.GitSourceError, mf.ManifestError, cas.StoreError) as e:
        r.problem("files", str(e))
    if tree is not None:
        try:
            mf.check_files(m, list(tree.files), src.where, strict=True)        # a waiver for a cell it lacks
        except mf.ManifestError as e:
            r.problem("checks", str(e))
    out["tree"] = tree
    limit = file_max()
    files = tree.files if tree else {}
    sizes = {p: cas.object_path(hx).stat().st_size for p, (_, hx) in files.items()}
    over = sorted(({"path": p, "bytes": n} for p, n in sizes.items() if n > limit), key=lambda x: -x["bytes"])
    largest = max(sizes.items(), key=lambda kv: kv[1]) if sizes else None
    out["files"] = {"count": len(files), "bytes": sum(sizes.values()), "limit": limit,
                    "largest": {"path": largest[0], "bytes": largest[1]} if largest else None, "over": over}
    for x in over:
        r.problem("files", f"{x['path']}: {x['bytes']} bytes, more than the {limit} a package file may have "
                           "(UNDINE_PKG_FILE_MAX)")
    # licence
    why = spdx.problem(m.license) if m.license else "no package.license: a licence (an SPDX expression) is required"
    out["license"] = {"expression": m.license, "ok": why is None,
                      "ids": spdx.ids(spdx.parse(m.license)) if why is None else []}
    if why:
        r.problem("license", why)
    # the PDK
    pp = pkgsource.project_pdk(src.prj)
    out["pdk"] = {"family": m.pdk.family if m.pdk else None, "variants": list(m.pdk.variants) if m.pdk else [],
                  "built_with": m.pdk.built_with if m.pdk else None, "project": pp["commit"] if pp else None}
    if m.pdk is None or not m.pdk.built_with:
        r.problem("pdk", "no pdk.built_with: name the PDK commit the layouts were drawn and checked on")
    elif pp is None or pp["commit"] is None:
        r.problem("pdk", "the project's PDK commit is not known here: `python -m undine pdk verify PROJECT --pin` "
                         "records it")
    elif pp["commit"] != m.pdk.built_with:
        r.problem("pdk", f"the project's PDK is {pp['commit'][:12]}, pdk.built_with is {m.pdk.built_with[:12]}: "
                         "check on the PDK the package names (set-pdk), or change built_with")
    elif pp["variant"] not in m.pdk.variants:
        r.problem("pdk", f"the project's PDK variant {pp['variant']} is not among pdk.variants")
    out["dependencies"] = _dependencies(r, src, index_url, offline)
    libs = src.prj.libraries()
    cells = _cells(files)
    out["cells"] = {c: _cell_entry(c, v, m) for c, v in cells.items()}
    out["code"] = {"blocks": [], "rtl": []}
    if tree is not None:
        tmp = Path(tempfile.mkdtemp(prefix="undine-check-"))
        try:
            cas.install(files, tmp / "pkg")
            _local_names(r, src, tmp / "pkg", libs)
            allowed, env = _model_env(src.prj.pdk)
            out["code"] = trust.listing(tmp / "pkg", allowed=allowed, env=env)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    refused = sum(1 for b in out["code"]["blocks"] if b["refused"]) + sum(1 for x in out["code"]["rtl"]
                                                                          if x["refused"])
    if refused:
        r.warn("code", f"{refused} code block(s) or RTL file(s) the trust rule refuses in a dependency until a "
                       "consumer trusts the package: listed under code")
    return out


def _cell_entry(cell: str, views: list[str], m: mf.Manifest) -> dict:
    exported = m.exports is None or cell in m.exports
    layout, sch = "layout" in views, "schematic" in views
    waived = [{"rule": rule, "count": None, "reason": w.reason}
              for w in (m.checks.waive if m.checks else ()) if w.cell == cell for rule in w.rules]
    deck = m.checks.deck if m.checks else None
    options = dict(m.checks.options) if m.checks else {}
    return {
        "exported": exported, "views": views,
        "symbol": {"state": "not_run", "applies": exported, "passed": False, "problems": []},
        "netlist": {"state": "not_run", "applies": exported and sch, "passed": False, "warnings": []},
        "drc": {"state": "not_run", "applies": layout, "deck": deck, "options": options, "job": None, "total": None,
                "counted": None, "waived": waived, "passed": False},
        "lvs": {"state": "not_run", "applies": layout and sch, "job": None, "match": None, "passed": False,
                **({} if layout and sch else {"why": "no layout" if not layout else "no schematic"})},
        "specs": {"state": "not_run", "applies": exported, "passed": False, "results": []},
    }


# ------------------------------------------------------------------ the report


_USERINFO = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://)[^/\s@'\"]+@")
_PATH_END = r"(?=$|[/\s'\"():,;\]}>])"          # a path ends at a separator, never inside a longer name


def public_url(url, local: str = "<local index>"):
    """A URL fit for the published report: user and password dropped (https://user:token@host/... -> https://host/...),
    a local path (or file:// URL) replaced by `local`. Anything else as it is."""
    if not isinstance(url, str):
        return url
    if url.startswith(("/", "~", ".", "file:")) or re.match(r"^[A-Za-z]:[\\/]", url):
        return local
    return _USERINFO.sub(r"\1", url)


def _path_subs(src) -> list[tuple[re.Pattern, str]]:
    """(pattern, placeholder) for the machine's paths a report must not hold: the repository, the project, Undine's
    home and the user's home, each as an absolute path (as given and resolved), never a relative one, and matched
    only as a whole path or a path's leading part, so a name that happens to equal a directory is left alone."""
    from undine.home import home
    out: dict[str, str] = {}
    for p, label in ((src.top, "<repository>"), (src.prj.path, "<project>"), (home(), "<undine home>"),
                     (Path.home(), "~")):
        if not p:
            continue
        for q in (Path(p).absolute(), Path(p).resolve()):
            s = str(q)
            if q.is_absolute() and len(s) > 1 and s not in out:
                out[s] = label
    return [(re.compile(re.escape(s) + _PATH_END), label) for s, label in sorted(out.items(), key=lambda x: -len(x[0]))]


def _scrub(obj, subs: list[tuple[re.Pattern, str]], exact: dict[str, str]):
    """Every string with the machine's paths replaced by a placeholder, the raw URLs in `exact` by their public form,
    and a user and password dropped from any URL: the report is published."""
    if isinstance(obj, str):
        for a, b in exact.items():
            obj = obj.replace(a, b)
        for rx, b in subs:
            obj = rx.sub(b, obj)
        return _USERINFO.sub(r"\1", obj)
    if isinstance(obj, list):
        return [_scrub(x, subs, exact) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub(v, subs, exact) for k, v in obj.items()}
    return obj


def build(src, tag: str, commit: str, *, index_url: str, offline: bool = False, run: bool = False) -> dict:
    """The report for the package `src` at its release `tag` / `commit`. run=False (`pkg check --no-run`): DRC, LVS,
    symbols, netlists and specs are not run, so runs.state is not_run and the report does not pass."""
    from undine import tools
    r = _Report()
    s = static(r, src, tag, commit, index_url=index_url, offline=offline)
    m = src.manifest
    tree = s.pop("tree")
    if not run:
        r.problem("runs", "DRC, LVS, the symbol and netlist checks and the specs were not run (--no-run): a "
                          "report passes only with them")
    report = {
        "format": FORMAT, "kind": KIND, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "package": {"name": m.name, "version": str(m.version), "repository": m.repository, "subdir": src.subdir,
                    "tag": tag, "commit": commit, "tree": tree.hash if tree else None},
        "pdk": s["pdk"], "tools": tools.tools_record(),
        "publisher_project": {"name": src.prj.name, "lib": src.lib.name},
        "files": s["files"], "license": s["license"], "dependencies": s["dependencies"], "cells": s["cells"],
        "runs": {"state": "done" if run else "not_run", **({} if run else {"why": "--no-run"})},
        "semver": {"state": "not_run", "previous": None, "actual": None, "required": None, "changes": [],
                   "specs_changed": [], "passed": False},
        "code": s["code"], "warnings": r.warnings, "problems": r.problems, "passed": not r.problems,
    }
    exact: dict[str, str] = {}                       # an index or git URL as written -> as published
    for row in report["dependencies"]:
        for key, local in (("index", "<local index>"), ("git", "<local repository>")):
            raw = row.get(key)
            if isinstance(raw, str) and (pub := public_url(raw, local)) != raw:
                exact[raw] = row[key] = pub
    exact = dict(sorted(exact.items(), key=lambda x: -len(x[0])))
    return _scrub(report, _path_subs(src), exact)


def dumps(report: dict) -> bytes:
    return (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write(path: Path, report: dict) -> str:
    """Writes the report (through a temporary file beside it); returns the SHA-256 of the bytes written."""
    data = dumps(report)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".new")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return hashlib.sha256(data).hexdigest()


def verify(data: bytes, line, sha256: str | None) -> list[str]:
    """The problems with a report for the index line `line` (store.index.Line), [] when there are none: the bytes'
    SHA-256 is not `sha256` (when given), the report is not format 1 of kind undine-check, it names another package,
    version, commit or tree than the line, or it does not pass."""
    out = []
    got = hashlib.sha256(data).hexdigest()
    if sha256 is not None and got != sha256:
        out.append(f"the report's SHA-256 is {got}, the line says {sha256}")
    try:
        rep = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        return out + [f"the report is not JSON ({e})"]
    if not isinstance(rep, dict):
        return out + ["the report is not a JSON object"]
    if rep.get("kind") != KIND:
        out.append(f"the report's kind is {rep.get('kind')!r}, not {KIND!r}")
    if rep.get("format") != FORMAT:
        out.append(f"the report's format is {rep.get('format')!r}; this Undine reads format {FORMAT}"
                   + (" (made by a newer Undine?)" if isinstance(rep.get("format"), int)
                      and rep["format"] > FORMAT else ""))
        return out
    pkg = rep.get("package") if isinstance(rep.get("package"), dict) else {}
    for key, want in (("name", line.name), ("version", str(line.vers)), ("commit", line.commit),
                      ("tree", line.tree)):
        if pkg.get(key) != want:
            out.append(f"the report is for {key} {pkg.get(key)!r}, the line names {want!r}")
    problems = rep.get("problems")
    if rep.get("passed") is not True:
        out.append("the report does not pass" + (f": {len(problems)} problem(s), the first: "
                                                  f"{_message(problems[0])}" if isinstance(problems, list)
                                                  and problems else ""))
    elif problems:
        out.append("the report says it passes but lists problems")
    return out


def _message(p) -> str:
    return p.get("message", str(p)) if isinstance(p, dict) else str(p)
