"""The review of a dependency change in a changeset (docs/design/packages.md, Changesets and evidence; phase 4).

A dependency operation of a changeset (dep_add, dep_update, dep_remove, pdk_set) records, in its `{k: "d"}` effect,
project.json and undine.lock before and after it. That says what changed in the files, not what came with it. `record`
makes what a person reviews of it, from that effect, the store and the working copy's own cells, once the operation
is recorded and its guard has let the changesets' and the server's locks go (Changesets._review_records; nothing here
takes a server lock or waits for an index refresh), and the changeset keeps it by the effect's `fingerprint`:

    packages     one row per lock entry whose content the operation changed (added, moved, removed): its package,
                 local names, version and commit before and after, its hash after (what a check's `about` names),
                 the licence (the manifest's, else the index copy's), the size (the index line's, else the files'
                 bytes in the store), the files added, removed and modified, and per cell what changed (added,
                 removed, renamed by the manifest's [renames], changed: which views), from the store's records
    used_by      the cells of the project's own libraries that place a changed or removed cell (deps._used_by's rule)
    code         per package added or moved: its key (what a person trusts), every code block of its cells and every
                 RTL file with what the jobs' trust rule refuses in them (trust.listing with the PDK root and the
                 netlister's variables); `keys` every package's key, for the trust note over all of them

The trees are read from the store's objects (linked into a temporary directory), not from the working copy's
installs, which follow the latest operation: the review is the operation's whenever it is made. Every list is
bounded (a count and the first rows), and every text that comes from a package (paths, cell names, code, findings,
licences, versions) has its control characters taken out and is cut, since the review is shown to people and read by
agents: it is data, never instructions.

Reading (Changesets.get): `rows` turns the changeset's project-log records and their reviews into the review's `deps`
rows, with the state of each operation, the semantic diffs of both files (depmerge.diff), whether the real project
trusts each package now, the keys the operation took out of project.json's `trusted`, and `checked`: whether a run in
the changeset (an LVS, a DRC or a simulation whose evidence names the package's tree in its `about`) has checked it.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from undine.store import cas, resolve

LISTED_PACKAGES = 10      # package rows a review records at most (the rest counted)
LISTED_FILES = 20         # files per list (added, removed, modified) of a package
LISTED_CELLS = 30         # cells per package
LISTED_USED_BY = 20       # cells that place a changed cell
LISTED_BLOCKS = 20        # code blocks per review, over its packages
LISTED_RTL = 20           # RTL files per review, over its packages
LISTED_FINDINGS = 3       # findings per RTL file
LISTED_KEYS = 200         # package keys a review records for its trust note (more: never called trusted)
TEXT = 200                # characters of a text from a package at most
CHECK_JOBS = 20           # runs of the working copy looked at for `checked`, newest first
# `trust` of a row and of a package's code: the trust note in words (agents read these, and only a person trusts)
TRUST_WORDS = {True: "trusted in the project", False: "not trusted: a job that runs its code is refused until a person "
                                                      "trusts this version"}

_VIEWS = {".sch": "schematic", ".sym": "symbol", ".oas": "layout", ".gds": "layout", ".v": "rtl", ".sv": "rtl",
          ".svh": "rtl", ".vh": "rtl"}
_COMPOUND = {".netlist.yaml": "netlist", ".intent.yaml": "intent", ".intent.json": "intent"}


def data(v, n: int = TEXT):
    """A text from a package as the review keeps it: control characters made spaces, lone surrogates U+FFFD, at most
    `n` characters (an ellipsis when cut). Anything but a string as it is (None, numbers)."""
    if not isinstance(v, str):
        return v
    t = re.sub(r"[\ud800-\udfff]", "�", re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", v)).strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def _listed(items: list, n: int) -> tuple[list, int]:
    return items[:n], max(0, len(items) - n)


# ------------------------------------------------------------------ recorded with the operation


def _hash(e: dict | None) -> str | None:
    if not isinstance(e, dict):
        return None
    from undine.store import deps
    kind, value = deps.entry_hash(e)
    return value if isinstance(value, str) else None


def _files(e: dict | None) -> dict[str, str] | None:
    """{path: object} of a lock entry's tree as the store records it; None when the store has no record of it."""
    if not isinstance(e, dict):
        return {}
    from undine.store import deps
    kind, value = deps.entry_hash(e)
    try:
        files = cas.read_tree(value) if kind == "tree" else cas.read_archive_tree(value)
    except (cas.StoreError, TypeError, ValueError):
        return None
    return {p: h for p, (_, h) in files.items()} if files is not None else None


def _bytes(files: dict[str, str] | None) -> int | None:
    if files is None:
        return None
    total = 0
    for h in set(files.values()):
        try:
            total += cas.object_path(h).stat().st_size
        except (OSError, cas.StoreError):
            return None
    return total


def _view(path: str) -> tuple[str, str] | None:
    """(cell, view) of a library file at the top of the tree, None for anything else (a README, a model file)."""
    if "/" in path:
        return None
    for suffix, view in _COMPOUND.items():
        if path.endswith(suffix):
            return path[:-len(suffix)], view
    p = Path(path)
    return (p.stem, _VIEWS[p.suffix]) if p.suffix in _VIEWS else None


def _renames(manifest, old: str | None, new: str | None) -> dict[str, str]:
    """{old cell: new cell} of the new version's [renames] for the versions after `old` up to `new`."""
    from undine.store import semver
    if manifest is None or not getattr(manifest, "renames", None):
        return {}
    try:
        lo = semver.Version.parse(old) if old else None
        hi = semver.Version.parse(new) if new else None
    except (semver.SemverError, ValueError, TypeError):
        return {}
    out: dict[str, str] = {}
    for ver, table in sorted(manifest.renames.items(), key=lambda kv: kv[0]):
        try:
            if (lo is not None and not lo < ver) or (hi is not None and hi < ver):
                continue
        except TypeError:
            continue
        for a, b in (table or {}).items():
            out = {k: (b if v == a else v) for k, v in out.items()}
            out.setdefault(str(a), str(b))
    return out


def _cells(a: dict[str, str] | None, b: dict[str, str] | None, renames: dict[str, str],
           exports) -> tuple[list[dict], int]:
    """Per cell, what an update did to it: {cell, do: add | del | mod | renamed, views, from?, exported}."""
    def by_cell(files):
        out: dict[str, dict[str, str]] = {}
        for p, h in (files or {}).items():
            if (cv := _view(p)) is not None:
                out.setdefault(cv[0], {})[cv[1] + ":" + p] = h
        return out
    old, new = by_cell(a), by_cell(b)
    rows: list[dict] = []
    gone = set(old) - set(new)
    for cell in sorted(set(old) | set(new)):
        if cell in old and cell in new:
            if old[cell] == new[cell]:
                continue
            keys = set(old[cell]) | set(new[cell])
            views = sorted({k.split(":", 1)[0] for k in keys if old[cell].get(k) != new[cell].get(k)})
            rows.append({"cell": cell, "do": "mod", "views": views})
        elif cell in new:
            was = next((o for o, n in renames.items() if n == cell and o in gone), None)
            if was is not None:
                rows.append({"cell": cell, "do": "renamed", "from": was,
                             "views": sorted({k.split(":", 1)[0] for k in new[cell]})})
            else:
                rows.append({"cell": cell, "do": "add", "views": sorted({k.split(":", 1)[0] for k in new[cell]})})
        elif renames.get(cell) not in new:
            rows.append({"cell": cell, "do": "del", "views": sorted({k.split(":", 1)[0] for k in old[cell]})})
    for r in rows:
        r["cell"] = data(r["cell"])
        if "from" in r:
            r["from"] = data(r["from"])
        if exports is not None:
            r["exported"] = r["cell"] in exports
    return _listed(rows, LISTED_CELLS)


def _file_changes(a: dict[str, str] | None, b: dict[str, str] | None) -> dict | None:
    """{added, removed, modified} (deps._diff_files' shape) from the store's records, None when one is not there."""
    if a is None or b is None:
        return None
    out: dict = {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
                 "modified": sorted(k for k in set(a) & set(b) if a[k] != b[k])}
    return out


def _bounded_files(ch: dict | None) -> dict | None:
    if ch is None:
        return None
    out: dict = {}
    for k in ("added", "removed", "modified"):
        rows, more = _listed([data(p) for p in ch[k]], LISTED_FILES)
        out[k] = rows
        if more:
            out[f"{k}_more"] = more
    return out


def _index_free(url) -> bool:
    """Whether the local copy of index `url` can be read now without waiting: no refresh holds its lock (Index.update
    holds it for its whole fetch). A review never waits for a network fetch: with the copy busy it takes the store's
    size and the manifest's licence only."""
    import fcntl

    from undine.store import index as ix
    if not isinstance(url, str):
        return False
    try:
        repo = ix._dir(ix._check_url(url))
    except Exception:
        return False
    f = repo.with_name(repo.name + ".lock")
    if not f.exists():
        return True
    try:
        with open(f, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return True
    except OSError:
        return False


def _entry_size(e: dict, files: dict[str, str] | None) -> tuple[int | None, str | None]:
    from undine.store import deps
    line = deps._index_line(e) if e.get("index") and _index_free(e.get("index")) else None
    if line is not None and line.size is not None:
        return line.size, "index"
    n = _bytes(files)
    return (n, "files") if n is not None else (None, None)


def _entry_license(e: dict, man) -> tuple[str | None, str | None]:
    """(licence, where from): the manifest's, else the local copy of the entry's index (vendor's rule), unless that
    copy is being refreshed."""
    from undine.store import vendor
    if man is not None and getattr(man, "license", None):
        return man.license, "manifest"
    if e.get("index") and _index_free(e.get("index")):
        try:
            got = vendor._index_license(e)
        except Exception:
            got = None
        if got:
            return got, "index"
    return None, None


@contextlib.contextmanager
def _tree_dir(prj, name: str, e: dict):
    """A directory holding exactly the lock entry's files, to read (manifest, code listing): linked from the store's
    objects into a temporary directory, so the review describes the tree the operation locked whatever the working
    copy installs since; else the working copy's install when it is that tree; else None."""
    from undine.store import deps
    files = None
    try:
        kind, value = deps.entry_hash(e)
        files = cas.read_tree(value) if kind == "tree" else cas.read_archive_tree(value)
    except (cas.StoreError, TypeError, ValueError):
        files = None
    if files is not None and not cas.missing_objects(files):
        tmp = Path(tempfile.mkdtemp(prefix=".review-", dir=cas.tmp_dir()))
        try:
            d = tmp / name
            d.mkdir()
            for rel, (_, h) in files.items():
                parts = rel.split("/")
                if any(x in ("", ".", "..") for x in parts) or rel.startswith("/"):
                    continue
                dst = d.joinpath(*parts)
                dst.parent.mkdir(parents=True, exist_ok=True)
                cas.link_or_copy(cas.object_path(h), dst)
            yield d
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return
    try:
        here = deps._current(prj.path, name, e)
    except Exception:
        here = False
    yield deps.dep_dir(prj.path, name) if here else None


def _used_by(prj, name: str, changes: dict[str, list[str]]) -> list[str]:
    """deps._used_by, reading a library's layout only when a layout cell of the package changed (a schematic is read
    as text; a layout is the whole file)."""
    from undine.store import deps
    changed = {Path(f).stem: Path(f).suffix for f in changes["modified"] + changes["removed"]}
    layouts = {c for c, ext in changed.items() if ext == ".oas"}
    if not changed:
        return []
    out = []
    for lname, spec in prj.library_specs().items():
        if deps.is_dep(spec):
            continue
        lib = prj.library(lname)
        if not lib.path.is_dir():
            continue
        for sch in sorted(lib.path.glob("*.sch")):
            text = sch.read_text(errors="replace")
            hits = sorted(c for c in changed if re.search(rf"\bC \{{{re.escape(name)}/{re.escape(c)}\.sym\}}", text))
            if hits:
                out.append(f"{lname}/{sch.stem} (schematic) places {', '.join(hits)}")
        if layouts:
            own = set(lib.layout_cells()[0])
            for c in sorted(layouts & own):
                out.append(f"{lname}/{c} (layout) is a copy of {name}/{c}: not updated, place it again to take the "
                           "new version")
    return out


def _key(man, names: list[str], e: dict) -> str:
    """What a person trusts for the entry (trust.key_of's rule): '<package>@<version>' from its manifest, else
    '<local name>@<commit12>'."""
    if man is not None:
        return f"{man.name}@{man.version}"
    commit = e.get("commit")
    ver = commit[:12] if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{12,64}", commit) else "unknown"
    return f"{names[0]}@{ver}"


def record(prj, before: dict, after: dict, allowed: list | None = None, env: dict | None = None) -> dict:
    """What the review of one dependency operation shows, from its effect (`before` and `after`: project.json and
    undine.lock parsed, as the effect keeps them), the store and the working copy `prj` (its own cells, for used_by).
    The trees are read from the store, so the review is the operation's whatever ran since. `allowed` and `env`: the
    PDK root and the netlister's variables, as the jobs' trust rule takes them (trust.check_netlist), so what is
    listed as refused is what a job refuses. Takes no lock of the server's and never waits for an index refresh: the
    caller runs it after the operation, outside the guard. See the module docstring; never raises (`error`)."""
    from undine.store import trust, vendor
    from undine.store import manifest as mf
    try:
        a_lock, b_lock = before.get("lock") or {}, after.get("lock") or {}
        ga, gb = resolve.lock_graph(a_lock), resolve.lock_graph(b_lock)
        ha = {_hash(e): id for id, e in ga.entries.items()}
        hb = {_hash(e): id for id, e in gb.entries.items()}
        moved: list[tuple[str | None, str | None]] = []        # (id before, id after)
        for id, e in gb.entries.items():
            if _hash(e) not in ha:
                prev = id if id in ga.entries else next((i for i in ga.entries if gb.names_of(id)[0] in
                                                         ga.names_of(i)), None)
                moved.append((prev if prev is not None and _hash(ga.entries[prev]) not in hb else None, id))
        for id, e in ga.entries.items():
            if _hash(e) not in hb and not any(p == id for p, _ in moved):
                moved.append((id, None))
        packages, code, used_by, keys = [], [], [], []
        for prev, now in moved:
            ea = ga.entries.get(prev) if prev else None
            eb = gb.entries.get(now) if now else None
            names = gb.names_of(now) if now else ga.names_of(prev)
            fa, fb = _files(ea), _files(eb)
            row: dict = {"package": data((eb or ea or {}).get("package")), "names": [data(n) for n in names],
                         "do": "add" if ea is None else "del" if eb is None else "mod",
                         "from": data((ea or {}).get("version") or (ea or {}).get("commit")),
                         "to": data((eb or {}).get("version") or (eb or {}).get("commit")),
                         "commit_from": data((ea or {}).get("commit")), "commit_to": data((eb or {}).get("commit")),
                         "hash": _hash(eb)}
            changes = _file_changes(fa if ea is not None else {}, fb if eb is not None else {})
            row["changed"] = _bounded_files(changes)
            if changes is not None and ea is not None:
                for n in names:
                    if len(used_by) >= LISTED_USED_BY * 2:
                        break
                    used_by += [data(u) for u in _used_by(prj, n, changes) if data(u) not in used_by]
            if eb is None:
                packages.append(row)
                continue
            with _tree_dir(prj, names[0], eb) as d:
                man = None
                if d is not None:
                    try:
                        man = mf.from_dir(d, names[0])
                    except (ValueError, OSError):
                        man = None
                lic, lic_from = _entry_license(eb, man)
                row["license"], row["license_from"] = data(lic), lic_from
                if fb:
                    row["license_files"] = [data(x) for x in vendor._license_files(fb)[:5]]
                row["size"], row["size_from"] = _entry_size(eb, fb)
                if ea is not None:
                    cells, more = _cells(fa, fb, _renames(man, ea.get("version"), eb.get("version")),
                                         man.exports if man is not None else None)
                    row["cells"] = cells
                    if more:
                        row["cells_more"] = more
                key = data(_key(man, names, eb))
                keys.append(key)
                if d is None:
                    code.append({"name": data(names[0]), "package": row["package"], "key": key,
                                 "error": "not in the store: its code could not be listed"})
                else:
                    try:
                        lst = trust.listing(d, allowed=allowed, env=env)
                        code.append({"name": data(names[0]), "package": row["package"], "key": key,
                                     "all": lst})
                    except Exception as err:          # what cannot be read is said, the other packages still listed
                        code.append({"name": data(names[0]), "package": row["package"], "key": key,
                                     "error": data(f"cannot list its code: {err}", 400)})
            packages.append(row)
        out = {"packages": None, "used_by": None, "code": None}
        out["packages"], more = _listed(packages, LISTED_PACKAGES)
        if more:
            out["packages_more"] = more
        out["used_by"], more = _listed(used_by, LISTED_USED_BY)
        if more:
            out["used_by_more"] = more
        out["code"], more = _code(code)
        if more:
            out["code_more"] = more
        # every package's key, for the trust note over all of them (a cut code list never hides an untrusted one)
        out["keys"], more = _listed(keys, LISTED_KEYS)
        if more:
            out["keys_more"] = more
        return out
    except Exception as e:                         # the review is information: the operation stands without it
        return {"error": data(f"the review could not be made: {e}", 400)}


def _code(entries: list[dict]) -> tuple[list[dict], int]:
    """The code entries as the review keeps them: packages with code refused first, then with code, then the others;
    at most LISTED_PACKAGES (the count of the rest), LISTED_BLOCKS blocks and LISTED_RTL RTL files over them."""
    def rank(c):
        lst = c.get("all") or {"blocks": [], "rtl": []}
        refused = sum(1 for b in lst["blocks"] if b.get("refused")) + sum(1 for r in lst["rtl"] if r.get("refused"))
        return (0 if c.get("error") or refused else 1 if lst["blocks"] or lst["rtl"] else 2)
    ordered = sorted(entries, key=rank)
    kept, more = _listed(ordered, LISTED_PACKAGES)
    blocks_left, rtl_left = LISTED_BLOCKS, LISTED_RTL
    out = []
    for c in kept:
        lst = c.pop("all", None)
        if lst is None:
            out.append(c)
            continue
        blocks, rtl = lst["blocks"], lst["rtl"]
        c |= {"blocks_total": len(blocks), "blocks_refused": sum(1 for b in blocks if b.get("refused")),
              "rtl_total": len(rtl), "rtl_refused": sum(1 for r in rtl if r.get("refused"))}
        # refused ones first: what the trust rule stops is what a person must read
        shown = sorted(blocks, key=lambda b: not b.get("refused"))[:max(0, blocks_left)]
        c["blocks"] = [{k: data(v) if k != "commands" else [data(x, 40) for x in v[:10]]
                        for k, v in b.items() if k in ("cell", "file", "where", "kind", "line", "text", "path",
                                                       "commands", "refused", "why")} for b in shown]
        blocks_left -= len(shown)
        shown_rtl = sorted(rtl, key=lambda r: not r.get("refused"))[:max(0, rtl_left)]
        c["rtl"] = [{"path": data(r["path"]), "refused": r["refused"],
                     "findings": [{k: data(v) for k, v in f.items()} for f in r["findings"][:LISTED_FINDINGS]],
                     **({"findings_more": len(r["findings"]) - LISTED_FINDINGS}
                        if len(r["findings"]) > LISTED_FINDINGS else {})} for r in shown_rtl]
        rtl_left -= len(shown_rtl)
        out.append(c)
    return out, more


# ------------------------------------------------------------------ read: the review's rows


def _diff(e: dict, kind: str) -> dict:
    from undine.vcs import depmerge

    def dump(side: dict | None) -> bytes | None:
        v = (side or {}).get(kind)
        return None if v is None else json.dumps(v).encode()
    try:
        return depmerge.diff(dump(e.get("before")), dump(e.get("after")), kind)
    except ValueError as err:
        return {"kind": kind, "changes": [], "error": data(str(err))}


def _trusted_list(project: dict | None) -> list[str]:
    t = (project or {}).get("trusted")
    return [k for k in t if isinstance(k, str)] if isinstance(t, list) else []


def _hashes(review: dict | None) -> set[str]:
    return {p["hash"] for p in (review or {}).get("packages") or [] if p.get("hash") and p.get("do") != "del"}


def _job_hashes(about: dict | None) -> set[str]:
    out = set()
    for p in (about or {}).get("packages") or []:
        for k in ("tree", "sha"):
            if isinstance(p.get(k), str):
                out.add(p[k])
    return out


def _what(st: dict, jid: str) -> dict:
    """{job, kind, lib?, cell?} of a run: its status's cell, else its first design entry's."""
    design = (st.get("inputs") or {}).get("design") or []
    first = {"lib": st["lib"], "cell": st["cell"]} if st.get("lib") and st.get("cell") else \
        next((d for d in design if isinstance(d, dict) and d.get("lib") and d.get("cell")), {})
    return {"job": jid, "kind": st.get("kind") or "sim",
            **({"lib": first["lib"], "cell": first["cell"]} if first else {})}


class Checks:
    """The runs that can check a dependency operation: those of the changeset's working copy (open: freshness asked
    of provenance), or of its evidence (closed: as they were archived). Read once per review."""

    def __init__(self, srv, work: str | None = None, evidence: Path | None = None, manifest: dict | None = None):
        self.srv, self.work, self.evidence, self.manifest = srv, work, evidence, manifest
        self._runs: list[tuple[str, dict, Path | None]] | None = None

    @property
    def runs(self) -> list[tuple[str, dict, Path | None]]:
        """(job id, status, its folder) of the done runs that name packages, newest first; read on first use (a
        changeset with no operation to check reads none)."""
        if self._runs is None:
            self._runs = self._read()
        return self._runs

    def _read(self) -> list[tuple[str, dict, Path | None]]:
        srv, work, evidence, manifest = self.srv, self.work, self.evidence, self.manifest
        runs: list[tuple[str, dict, Path | None]] = []
        if work is not None:
            try:
                d = srv.jobs.runs_dir(srv.project(work).path)
                for p in sorted(d.iterdir(), reverse=True):
                    try:
                        st = json.loads((p / "status.json").read_text())
                    except (OSError, ValueError):
                        continue
                    if isinstance(st, dict) and st.get("state") == "done" and (st.get("about") or {}).get("packages"):
                        runs.append((p.name, st, p))
            except (KeyError, ValueError, OSError):
                pass
        elif evidence is not None and manifest is not None:
            for j in reversed(manifest.get("jobs") or []):
                if j.get("side") != "work" or not (j.get("about") or {}).get("packages"):
                    continue
                try:
                    st = json.loads((evidence / j["path"] / "status.json").read_text())
                except (OSError, ValueError, KeyError, TypeError):
                    st = {}
                if isinstance(st, dict) and st.get("state") == "done":
                    runs.append((str(j.get("id")), {**st, "about": j.get("about")}, None))
        return runs

    def find(self, hashes: set[str]) -> dict | None:
        """The newest run that read one of these trees and (open) is still fresh: {job, kind, lib?, cell?}."""
        if not hashes:
            return None
        from undine.store import provenance
        looked = 0
        for jid, st, _ in self.runs:
            if not (_job_hashes(st.get("about")) & hashes):
                continue
            if self.work is not None:
                looked += 1
                if looked > CHECK_JOBS:
                    return None
                try:
                    if provenance.freshness(self.srv, self.work, st).get("fresh") is False:
                        continue
                except Exception:
                    continue
            return _what(st, jid)
        return None


def fingerprint(e: dict) -> str:
    """What a stored review is filed under: a hash of the effect's project.json and undine.lock before and after (the
    review is a function of them and of the store)."""
    body = json.dumps([e.get("before"), e.get("after")], sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _lock_hashes(lock: dict | None) -> set[str]:
    return {h for e in resolve.lock_graph(lock or {}).entries.values() if (h := _hash(e))}


def rows(lc_ops: list[dict], trusted_now: set[str], checks: Checks | None, review_of=None) -> list[dict]:
    """The review's `deps` rows: one per dependency operation of the changeset still in effect (the project log's
    records, `changes` of Changesets.get), oldest first. A proposal a person's fetch installed is the row of the
    record that installed it (with its op_id and fetched_by); a dropped one's record (dep_drop) is no row, the
    proposal's row says `outcome`. `review_of(effect)`: (its review, whether it was stored before this read), or
    (None, False).

    `checked` is judged on the trees of the operation that the changeset still has (the last operation's lock): an
    operation whose trees a later one replaced all of is not unchecked, it is `replaced_by` that later operation
    (checked null; that row says whether the package was checked)."""
    effs = [(op, e) for op in lc_ops for e in op.get("effects") or [] if e.get("k") == "d"]
    final = None
    for op, e in effs:
        if e.get("state") == "installed" and isinstance(e.get("after"), dict):
            final = e["after"].get("lock")
    live_hashes = _lock_hashes(final)
    out = []
    for op, e in effs:
        if e.get("state") == "dropped":
            continue
        if e.get("state") == "needs_fetch" and e.get("outcome") == "installed":
            continue
        row: dict = {"gseq": op.get("gseq"), "opid": op.get("opid"), "op": op.get("op"),
                     "action": e.get("action") or {"add": "add", "mod": "update", "del": "remove"}.get(e.get("do")),
                     "name": e.get("name"), "actor": op.get("actor"), "reason": op.get("reason") or "",
                     "t": op.get("t")}
        if op.get("op") == "pdk_set":
            row["action"] = "pdk_set"
            row["pdk"] = e.get("pdk")
        if e.get("state") == "needs_fetch":
            row["state"] = "cancelled" if e.get("outcome") == "cancelled" else "needs_fetch"
            if e.get("outcome") in ("superseded", "dropped"):
                row["outcome"] = e["outcome"]
            row["op_id"] = e.get("op_id")
            all_needs = e.get("needs") or []
            needs = [{k: data(f.get(k), 400 if k == "url" else TEXT)
                      for k in ("url", "rev", "commit", "subdir", "size", "license", "package", "version")}
                     for f in all_needs[:LISTED_PACKAGES]]
            row["needs"] = needs
            if len(all_needs) > LISTED_PACKAGES:
                row["needs_more"] = len(all_needs) - LISTED_PACKAGES
            first = needs[0] if needs else {}
            sizes = [f.get("size") for f in all_needs]
            row |= {"license": first.get("license"), "size": first.get("size"),
                    "size_from": "index" if first.get("size") is not None else None,
                    "size_total": sum(sizes) if sizes and all(isinstance(x, int) for x in sizes) else None,
                    "project": {"kind": "project", "changes": []}, "lock": {"kind": "lock", "changes": []},
                    "packages": [], "used_by": [], "code": None, "trusted": False, "trust_drop": [],
                    "checked": False}
            out.append(row)
            continue
        row["state"] = "installed"
        if e.get("op_id"):
            row["op_id"] = e["op_id"]
        if op.get("fetched_by"):
            row["fetched_by"] = op["fetched_by"]
        row["project"], row["lock"] = _diff(e, "project"), _diff(e, "lock")
        review, stored = review_of(e) if review_of is not None else (e.get("review"), "review" in e)
        row["recorded"] = bool(stored)
        review = review or {}
        if review.get("error"):
            row["review_error"] = review["error"]
        pkgs = review.get("packages") or []
        mine = next((p for p in pkgs if row["name"] in (p.get("names") or [])), pkgs[0] if pkgs else None)
        sizes = [p.get("size") for p in pkgs if p.get("do") != "del"]
        row |= {"packages": pkgs, "used_by": review.get("used_by") or [],
                "license": (mine or {}).get("license"), "size": (mine or {}).get("size"),
                "size_from": (mine or {}).get("size_from"),
                "size_total": sum(sizes) if sizes and all(isinstance(x, int) for x in sizes)
                and not review.get("packages_more") else None,
                # copies: stored reviews may be shared, and the trust is the reader's to add
                "code": [dict(c) for c in review.get("code") or []]}
        for k in ("packages_more", "used_by_more", "code_more"):
            if review.get(k):
                row[k] = review[k]
        for c in row["code"]:
            c["trusted"] = c.get("key") in trusted_now
            c["trust"] = TRUST_WORDS[c["trusted"]]
        # the trust note over every package the operation brought, listed in `code` or not
        keys = review.get("keys")
        if keys is None:                                # a review from before keys were kept: its code entries'
            keys = [c.get("key") for c in row["code"] if c.get("key")]
        untrusted = [k for k in keys if k not in trusted_now]
        complete = not review.get("keys_more") and (review.get("keys") is not None or not review.get("code_more"))
        row["trusted"] = bool(keys) and not untrusted and complete
        row["trust"] = TRUST_WORDS[row["trusted"]] if keys else None
        if untrusted:
            row["untrusted"] = untrusted[:10]
            row["untrusted_count"] = len(untrusted) + (review.get("keys_more") or 0)
        before_t = _trusted_list((e.get("before") or {}).get("project"))
        after_t = set(_trusted_list((e.get("after") or {}).get("project")))
        row["trust_drop"] = [data(k) for k in before_t if k not in after_t]
        hashes = _hashes(review)
        here = hashes & live_hashes
        if row["action"] in ("remove", "pdk_set") or not hashes:
            row["checked"] = None                       # nothing came in that a check could read
        elif not here:
            # every tree it brought was replaced by a later operation: that operation's row is the one to check
            row["checked"] = None
            later = next((o.get("gseq") for o, x in effs if (o.get("gseq") or 0) > (op.get("gseq") or 0)
                          and x.get("state") == "installed" and x.get("name") == e.get("name")), None)
            row["replaced_by"] = later if later is not None else next(
                (o.get("gseq") for o, x in effs if (o.get("gseq") or 0) > (op.get("gseq") or 0)
                 and x.get("state") == "installed"), None)
        else:
            by = checks.find(here) if checks is not None else None
            row["checked"] = by is not None
            if by is not None:
                row["checked_by"] = by
        out.append(row)
    return out
