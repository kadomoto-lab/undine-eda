"""The library browser's Packages section (docs/design/packages.md, UI, CLI, HTTP, agent tools): the indexes as the
browser reads them.

    search(query)        the newest version that is not yanked of each package whose name or description holds query
    info(name)           every version of a package, with what the newest says and whether it fits the project's PDK
    refresh(index)       fetch an index again
    newer(prj, rows)     per dependency from an index, the newest version its requirement accepts and the newest listed

Over HTTP an index is read under the same rule as a git dependency (deps._source): a local index only below
--dep-root, a remote one only on a host allowed_hosts gives (the configured indexes' hosts and --dep-host). The first
read of an index fetches it; search and info read the local copy after that, and only refresh fetches it again.
newer() reads the local copies as they are and never fetches: a dependency row is drawn often.
"""
from __future__ import annotations

from pathlib import Path

from undine.pdks import compat
from undine.store import deps
from undine.store import index as ix
from undine.store.semver import Req, SemverError, Version


def _index(url: str, local_roots: list[Path] | None, hosts, command: str) -> ix.Index:
    try:
        src = deps._source({"git": url}, local_roots, command, hosts) if local_roots is not None else None
    except deps.DepError as e:
        raise deps.DepError(f"index {url}: {e}") from None
    return ix.Index(url, src)


def _target(prj):
    if prj is None:
        return None
    lock = deps.read_lock(prj.path)
    pin = deps.lock_pdk(lock) if lock else ({"id": prj.pdk.id} if prj.pdk else None)
    return compat.project_target(pin, prj.pdk)


def _fits(line: ix.Line, target, prj) -> dict | None:
    """Whether the version accepts the project's PDK: {ok, reason}; None without a project."""
    if prj is None:
        return None
    req = line.pdk_req()
    if req is None:
        return {"ok": True, "reason": None}
    why = compat.check(req, target)
    return {"ok": why is None, "reason": why}


def _line(line: ix.Line, meta: dict | None) -> dict:
    return {"name": line.name, "version": str(line.vers), "description": line.description, "license": line.license,
            "yanked": line.yanked, "git": line.git, "subdir": line.subdir, "commit": line.commit, "tree": line.tree,
            "size": line.size, "pdk": line.pdk, "exports": list(line.exports) if line.exports is not None else None,
            "deps": [{"name": d.local, "package": d.package, "req": str(d.req),
                      "index": d.index, "index_name": ix.nickname(d.index, meta) if d.index else None}
                     for d in line.deps]}


def _where(url: str, meta: dict | None, idx: ix.Index) -> dict:
    return {"index": url, "index_name": ix.nickname(url, meta), "commit": idx.commit()}


def search(query: str = "", index: str | None = None, prj=None, local_roots: list[Path] | None = None,
           hosts=()) -> dict:
    """{index, index_name, commit, packages: [{name, version, description, license, yanked, fits}]}: the newest
    version of each package whose name or description holds `query` (every package for an empty one)."""
    meta = prj.meta if prj is not None else {}
    url = ix.url_of(index, meta)
    idx = _index(url, local_roots, hosts, f"pkg search {query}".rstrip())
    found = idx.search(query)
    target = _target(prj)
    rows = [{k: v for k, v in _line(x, meta).items() if k in ("name", "version", "description", "license", "yanked")}
            | {"fits": _fits(x, target, prj)} for x in found]
    return _where(url, meta, idx) | {"packages": rows}


def info(name: str, index: str | None = None, prj=None, local_roots: list[Path] | None = None, hosts=()) -> dict:
    """{index, index_name, commit, name, newest, versions: [line fields + fits], locked}: every version the index
    lists, newest first; `newest` the newest that is not yanked (else the newest); `locked` the version the project
    locks of this package from this index, or None. KeyError when the index lists no such package."""
    meta = prj.meta if prj is not None else {}
    url = ix.url_of(index, meta)
    idx = _index(url, local_roots, hosts, f"pkg info {name}")
    vs = idx.versions(name)
    if not vs:
        raise KeyError(f"{ix.nickname(url, meta)} lists no package {name}: search for a name, or refresh the index")
    target = _target(prj)
    rows = [_line(v, meta) | {"fits": _fits(v, target, prj)} for v in vs]
    newest = next((r for r in rows if not r["yanked"]), rows[0])
    locked = None
    if prj is not None:
        lock = deps.read_lock(prj.path) or {}
        for t in ("libraries", "packages"):
            for e in (lock.get(t) or {}).values():
                if isinstance(e, dict) and e.get("package") == name and e.get("index") == url:
                    locked = e.get("version")
    return _where(url, meta, idx) | {"name": name, "newest": newest["version"], "versions": rows, "locked": locked}


def refresh(index: str | None = None, prj=None, local_roots: list[Path] | None = None, hosts=()) -> dict:
    """Fetches the index again: {index, index_name, commit}."""
    meta = prj.meta if prj is not None else {}
    url = ix.url_of(index, meta)
    idx = _index(url, local_roots, hosts, f"pkg index update {url}")
    idx.update()
    return _where(url, meta, idx)


def indexes(prj=None) -> list[dict]:
    """The indexes known here and to the project: [{name, url, commit}] (commit None before the first fetch)."""
    meta = prj.meta if prj is not None else {}
    out = []
    for nick, url in ix.known(meta).items():
        try:
            commit = ix.Index(url).commit()
        except ix.IndexReadError:
            commit = None
        out.append({"name": nick, "url": url, "commit": commit})
    return out


def newer(prj, lock: dict | None = None) -> dict[str, dict]:
    """{project.json name: {accepted, newest}} for each direct dependency from an index with a newer version than the
    locked one in the local copy of its index: `accepted` the newest its requirement accepts (None when only a newer
    major is listed), `newest` the newest listed; yanked versions left out. Reads the local copies only."""
    lock = deps.read_lock(prj.path) if lock is None else lock
    out: dict[str, dict] = {}
    specs = prj.library_specs()
    for key, e in ((lock or {}).get("libraries") or {}).items():
        if not (isinstance(e, dict) and isinstance(e.get("index"), str) and isinstance(e.get("package"), str)):
            continue
        try:
            idx = ix.Index(e["index"])
            if not idx.fetched():
                continue
            have = Version.parse(e["version"])
            req = Req.parse((specs.get(key) or {}).get("version") or "*")
            vs = [v for v in idx.versions(e["package"]) if not v.yanked]
        except (ix.IndexReadError, SemverError, ValueError, TypeError, KeyError):
            continue
        newest = vs[0].vers if vs else None
        accepted = next((v.vers for v in vs if req.matches(v.vers)), None)
        if (newest is not None and newest > have) or (accepted is not None and accepted > have):
            out[key] = {"accepted": str(accepted) if accepted is not None and accepted > have else None,
                        "newest": str(newest) if newest is not None and newest > have else None}
    return out
