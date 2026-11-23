"""The review of a dependency change in a changeset (docs/design/packages.md, Changesets and evidence; phase 4).

A dependency operation of a changeset (dep_add, dep_update, dep_remove, pdk_set) records, in its `{k: "d"}` effect,
project.json and undine.lock before and after it. That says what changed in the files, not what came with it. `record`
makes what a person reviews of it, from that effect, the store and the working copy's own cells, once the operation
is recorded and its guard has let the changesets' and the server's locks go (Changesets._review_records; nothing here
takes a server lock or waits for an index refresh), and the changeset keeps it, one per operation:

    packages     one row per lock entry whose content the operation changed (added, moved, removed): its package,
                 local names, version and commit before and after, its hash after (what a check's `about` names),
                 the licence (the manifest's, else the index copy's), the size (the index line's, else the files'
                 bytes in the store), the files added, removed and modified, and per cell what changed (added,
                 removed, renamed by the manifest's [renames], changed: which views), from the store's records
    used_by      the cells of the project's own libraries that place a changed or removed cell (deps._used_by's rule)
    code         per package added or moved: its key (what a person trusts), every code block of its cells and every
                 RTL file with what the jobs' trust rule refuses in them (trust.listing with the PDK root and the
                 netlister's variables), for the packages that have any

The trees are read from the store's objects (linked into a temporary directory), not from the working copy's
installs, which follow the latest operation: the review is the operation's whenever it is made. Every list is
bounded (a count and the first rows), and every text that comes from a package (paths, cell names, code, findings,
licences, versions) has its control characters taken out and is cut, since the review is shown to people and read by
agents: it is data, never instructions.

Reading (Changesets.get): `rows` turns the changeset's project-log records and their reviews into the review's `deps`
rows, with the state of each operation, the semantic diffs of both files (depmerge.diff), whether the real project
trusts every version it brought now (read from the effect's lock, as are the trees `checked` looks for, so a row
whose review is missing or failed never looks safer), the keys the operation took out of project.json's `trusted`, and `checked`: whether a run in
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
LISTED_UNTRUSTED = 10     # untrusted versions a row names (untrusted_count counts them all)
LISTED_KEYS = 1000        # entries whose trust keys a review keeps (the others are read from the store again)
TEXT = 200                # characters of a text from a package at most
CHECK_JOBS = 20           # runs of the working copy looked at for `checked`, newest first
# `trust` of a row and of a package's code: the trust note in words (agents read these, and only a person trusts)
TRUST_WORDS = {True: "trusted in the project", False: "not trusted: a job that runs its code is refused until a person "
                                                      "trusts this version"}
TRUST_REPLACED = ("replaced: a later operation of this changeset replaced this version, so its code is no longer in "
                  "the changeset; the later operation's row says the trust of what it brings")
TRUST_CODE_TAKEN_BACK = ("trusted by its version now, so runs in this changeset still run this code; the adopt takes "
                         "that trust back (other content under the same version), after which a run of it is refused "
                         "until a person trusts it again")

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


@contextlib.contextmanager
def _index_held(url):
    """Holds the local copy of index `url` for reading without ever waiting: a shared lock on its lock file taken with
    LOCK_NB and kept for the whole read, so a refresh (Index.update holds the lock exclusively for its whole fetch) can
    neither be under way nor start meanwhile; the read's own shared lock (Index.versions) is then granted at once.
    Yields whether it is held; False (a refresh under way, a URL that is not one): the caller reads no index and takes
    the store's size and the manifest's licence."""
    import fcntl

    from undine.store import index as ix
    fh = None
    try:
        repo = ix._dir(ix._check_url(url)) if isinstance(url, str) else None
        if repo is not None:
            repo.parent.mkdir(parents=True, exist_ok=True)
            fh = open(repo.with_name(repo.name + ".lock"), "a")
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except Exception:
        if fh is not None:
            fh.close()
        fh = None
    try:
        yield fh is not None
    finally:
        if fh is not None:
            fh.close()                       # closing the file lets the lock go


def _entry_size(e: dict, files: dict[str, str] | None) -> tuple[int | None, str | None]:
    from undine.store import deps
    line = None
    if e.get("index"):
        with _index_held(e.get("index")) as held:
            try:
                line = deps._index_line(e) if held else None
            except Exception:
                line = None
    if line is not None and line.size is not None:
        return line.size, "index"
    n = _bytes(files)
    return (n, "files") if n is not None else (None, None)


def _entry_license(e: dict, man) -> tuple[str | None, str | None]:
    """(licence, where from): the manifest's, else the local copy of the entry's index (vendor's rule), unless that
    copy is being refreshed."""
    if man is not None and getattr(man, "license", None):
        return man.license, "manifest"
    if e.get("index"):
        with _index_held(e.get("index")) as held:
            got = _index_license(e) if held else None
        if got:
            return got, "index"
    return None, None


def _index_license(e: dict) -> str | None:
    """vendor._index_license's answer, read with fetch=False: inside the review an index copy is only read, never
    fetched or updated (an update would wait for the shared lock the review holds: for good)."""
    from undine.store import index as ix
    url, package, version = e.get("index"), e.get("package"), e.get("version")
    if not (isinstance(url, str) and isinstance(package, str) and isinstance(version, str)):
        return None
    try:
        idx = ix.Index(url, fetch=False)
        if not idx.fetched():
            return None
        return next((v.license for v in idx.versions(package) if str(v.vers) == version), None)
    except Exception:
        return None


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


_UNREAD = object()


_MANIFESTS: dict[tuple[str, str], object] = {}        # (kind, hash) -> (name, version) or None, once known for good


def _manifest_id(kind: str, value: str):
    """(name, version) of the manifest of the tree a lock entry names, read from the store; None when the tree has no
    manifest, or one that does not parse (trust._identity reads it the same way: such a tree is known by its
    commit); _UNREAD when that cannot be told now (the store has no record of the tree, or not the manifest's object:
    `store verify` removed it, `lib sync` brings it back). A tree is content addressed, so what was told is kept; an
    _UNREAD is not (the next read looks again)."""
    from undine.store import manifest as mf
    if (kind, value) in _MANIFESTS:
        return _MANIFESTS[(kind, value)]
    try:
        files = cas.read_tree(value) if kind == "tree" else cas.read_archive_tree(value)
    except (cas.StoreError, TypeError, ValueError):
        files = None
    if files is None:
        return _UNREAD
    got = files.get(mf.MANIFEST)
    if got is None:
        out = None
    else:
        try:
            data = cas.object_path(got[1]).read_bytes()
        except (OSError, cas.StoreError):
            return _UNREAD                               # its object is not there: not "no manifest"
        try:
            m = mf.parse(data, "review")
            out = (m.name, str(m.version))
        except ValueError:
            out = None
    if len(_MANIFESTS) > 8192:
        _MANIFESTS.clear()
    _MANIFESTS[(kind, value)] = out
    return out


UNKNOWN_KEY = "?"            # the version part of a key that cannot be told now (entry_keys)


def entry_keys(names: list[str], e: dict, man=_UNREAD) -> list[str]:
    """What a person trusts for a lock entry, exactly as trust.key_of gives it for its installs (the jobs, the trust
    dialog, `lib trust` and POST /trust use that): '<name>@<version>' from the tree's manifest, whatever the lock
    format records, else '<local name>@<commit12>' for each local name it is installed under (the install marker's
    commit, the lock entry's). `man`: the manifest when the caller has read it; else it is read from the store, and,
    when that cannot be told, the lock's package and version stand in (format 2), else '<local name>@?' for each
    name: a key nobody trusts, so the row says it is not known rather than naming one the jobs do not use."""
    if man is _UNREAD:
        try:
            from undine.store import deps
            kind, value = deps.entry_hash(e)
            got = _manifest_id(kind, value) if isinstance(value, str) else _UNREAD
        except Exception:
            got = _UNREAD
        if got is _UNREAD and isinstance(e.get("package"), str) and isinstance(e.get("version"), str):
            got = (e["package"], e["version"])
        if got is _UNREAD:
            return [f"{n}@{UNKNOWN_KEY}" for n in names]
    else:
        got = (man.name, str(man.version)) if man is not None else None
    if isinstance(got, tuple):
        return [f"{got[0]}@{got[1]}"]
    commit = e.get("commit")
    ver = commit[:12] if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{12,64}", commit) else "unknown"
    return [f"{n}@{ver}" for n in names]


def _entry_id(eb: dict, names: list[str]) -> str:
    """What a review's `keys` files an entry's keys under: its tree hash and its local names."""
    return f"{_hash(eb)}|{','.join(names)}"


def changed(before: dict, after: dict) -> list[tuple[dict | None, dict | None, list[str]]]:
    """(entry before, entry after, local names) of every lock entry whose content an effect changed (`before` and
    `after`: project.json and undine.lock as the effect keeps them): added, moved or removed. Entries are paired by
    who they are (their lock identity, else a local name they share), never by their content: two local names may
    lock the same tree, and each is its own install, trust key and removal."""
    ga, gb = resolve.lock_graph((before or {}).get("lock") or {}), resolve.lock_graph((after or {}).get("lock") or {})
    out: list[tuple[dict | None, dict | None, list[str]]] = []
    paired: set[str] = set()
    for id, e in gb.entries.items():
        names = gb.names_of(id)
        prev = id if id in ga.entries else next((i for i in ga.entries if i not in paired and names[0] in
                                                 ga.names_of(i)), None)
        if prev is not None:
            paired.add(prev)
            if _hash(ga.entries[prev]) == _hash(e) and ga.names_of(prev) == names:
                continue                                 # the same entry, the same content: not changed
        out.append((ga.entries.get(prev) if prev is not None else None, e, names))
    for id, e in ga.entries.items():
        if id not in paired:
            out.append((e, None, ga.names_of(id)))
    return out


def _listing(prj, names: list[str], eb: dict, d, package, allowed, env, man=_UNREAD) -> dict:
    """One package's code entry: every code block and RTL file (trust.listing with the jobs' PDK root and variables),
    or why it could not be listed."""
    from undine.store import trust
    keys = [data(k) for k in entry_keys(names, eb, man)]
    key = keys[0]
    if d is None:
        return {"name": data(names[0]), "package": package, "key": key, **({"keys": keys} if len(keys) > 1 else {}),
                "error": "not in the store: its code could not be listed"}
    try:
        return {"name": data(names[0]), "package": package, "key": key, **({"keys": keys} if len(keys) > 1 else {}),
                "all": trust.listing(d, allowed=allowed, env=env)}
    except Exception as err:                  # what cannot be read is said, the other packages still listed
        return {"name": data(names[0]), "package": package, "key": key, **({"keys": keys} if len(keys) > 1 else {}),
                "error": data(f"cannot list its code: {err}", 400)}


def code_listing(prj, before: dict, after: dict, allowed: list | None = None, env: dict | None = None) -> dict:
    """The code part of a review alone ({code, code_more?}), for another PDK than the one it was made with: what the
    trust rule refuses depends on the PDK root and its variables. Raises what it cannot read (the caller keeps the
    review it had)."""
    entries = []
    for ea, eb, names in changed(before, after):
        if eb is not None:
            with _tree_dir(prj, names[0], eb) as d:
                entries.append(_listing(prj, names, eb, d, data(eb.get("package")), allowed, env))
    code, more = _code(entries)
    return {"code": code, **({"code_more": more} if more else {})}


def record(prj, before: dict, after: dict, allowed: list | None = None, env: dict | None = None,
           name: str | None = None) -> dict:
    """What the review of one dependency operation shows, from its effect (`before` and `after`: project.json and
    undine.lock parsed, as the effect keeps them), the store and the working copy `prj` (its own cells, for used_by).
    The trees are read from the store, so the review is the operation's whatever ran since. `allowed` and `env`: the
    PDK root and the netlister's variables, as the jobs' trust rule takes them (trust.check_netlist), so what is
    listed as refused is what a job refuses. Takes no lock of the server's and never waits for an index refresh: the
    caller runs it after the operation, outside the guard. See the module docstring; never raises: a review that could
    not be made is {"error"}, which the caller does not keep as final."""
    from undine.store import vendor
    from undine.store import manifest as mf
    try:
        packages, code, used_by, keys = [], [], [], {}
        for ea, eb, names in changed(before, after):
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
                code.append(_listing(prj, names, eb, d, row["package"], allowed, env,
                                     man if d is not None else _UNREAD))
                # the keys made from the tree read now, kept: a later read (the store gc'd) names the same ones
                if d is not None and len(keys) < LISTED_KEYS:
                    keys[_entry_id(eb, names)] = [data(k) for k in entry_keys(names, eb, man)]
            packages.append(row)
        # the named dependency's own rows first, so a cut never takes them (resolve lists the packages it needs
        # first); its licence and size kept apart too, for the row (`own`), whatever the list keeps
        mine = [p for p in packages if name is not None and name in (p.get("names") or [])]
        packages = mine + [p for p in packages if p not in mine]
        own = next((p for p in mine if p.get("do") != "del"), None)
        out = {"packages": None, "used_by": None, "code": None,
               "own": {k: own.get(k) for k in ("package", "license", "license_from", "size", "size_from")}
               if own is not None else None}
        out["packages"], more = _listed(packages, LISTED_PACKAGES)
        if more:
            out["packages_more"] = more
        out["used_by"], more = _listed(used_by, LISTED_USED_BY)
        if more:
            out["used_by_more"] = more
        out["code"], more = _code(code)
        if more:
            out["code_more"] = more
        out["keys"] = keys
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
    # a package with no code block and no RTL file has nothing to list: it is no entry (its trust is the row's, from
    # the lock), so code_more counts packages with code that are not listed
    ordered = [c for c in sorted(entries, key=rank) if rank(c) < 2]
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
    """A hash of the effect's project.json and undine.lock before and after: a kept review carries it, so a file is
    only taken for the effect it was made for."""
    body = json.dumps([e.get("before"), e.get("after")], sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _lock_hashes(lock: dict | None) -> set[str]:
    return {h for e in resolve.lock_graph(lock or {}).entries.values() if (h := _hash(e))}


def _package_of(name, lock: dict | None, project: dict | None) -> str | None:
    """The package a dependency's local name stands for: its lock entry's, else its project.json requirement's."""
    if not isinstance(name, str):
        return None
    e = ((lock or {}).get("libraries") or {}).get(name)
    if isinstance(e, dict) and isinstance(e.get("package"), str):
        return e["package"]
    spec = ((project or {}).get("libraries") or {}).get(name)
    return spec.get("package") if isinstance(spec, dict) and isinstance(spec.get("package"), str) else None


def _git_of(e: dict, op: dict, lock: dict | None, project: dict | None) -> tuple[str | None, str | None]:
    """(git URL, subdir) of the dependency a waiting operation names: what the proposal recorded, its request's, the
    working copy's lock entry's or project.json requirement's for its local name."""
    name = e.get("name")
    args = op.get("args") or {}
    for src in (e, args, ((lock or {}).get("libraries") or {}).get(name),
                ((project or {}).get("libraries") or {}).get(name)):
        if isinstance(src, dict) and isinstance(src.get("git"), str):
            return src["git"], src.get("subdir")
    return None, None


def _same_url(a, b) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.rstrip("/") == b.rstrip("/")


def _keys_text(keys: list[str]) -> str:
    shown = ", ".join(keys[:LISTED_UNTRUSTED])
    return shown + (f" and {len(keys) - LISTED_UNTRUSTED} more" if len(keys) > LISTED_UNTRUSTED else "")


def trust_words(refused: list[str], back: list[str], replaced: bool, gone_keys: list[str] = ()) -> str:
    """The row's trust note in words, true for each version it names: those not trusted now (a run of their code is
    refused now) first, then those trusted now that this change takes back (runs in the changeset still run them until
    the adopt), then those a later operation replaced (no longer in the changeset); a row all of whose versions were
    replaced says no more than that."""
    if replaced:
        return TRUST_REPLACED
    parts = []
    if refused:
        parts.append(f"not trusted: {_keys_text(refused)} not trusted in the project now, so a run of that code is "
                     "refused until a person trusts it")
    if back:
        parts.append(f"{'' if refused else 'not trusted once adopted: '}{_keys_text(back)} trusted now, but this "
                     "change takes that trust back (other content under the same version): runs in this changeset "
                     "still run that code until the adopt, and after it a run of it is refused until a person "
                     "trusts it again")
    if gone_keys:
        parts.append(f"{_keys_text(list(gone_keys))} replaced by a later operation of this changeset (no longer in it)")
    return "; ".join(parts) if parts else TRUST_WORDS[True]


def rows(lc_ops: list[dict], trusted_now: set[str], checks: Checks | None, review_of=None,
         is_open: bool = True, work_lock: dict | None = None, work_project: dict | None = None) -> list[dict]:
    """The review's `deps` rows: one per dependency operation of the changeset still in effect (the project log's
    records, `changes` of Changesets.get), oldest first. A proposal a person's fetch installed is the row of the
    record that installed it (with its op_id and fetched_by); a dropped one's record (dep_drop) is no row, the
    proposal's row says `outcome`. `review_of(op, index, effect)`: (its review or None, its state: "stored" (kept
    with the operation), "made" (made by this read), "pending" (not made yet: this read may not make it) or "failed"
    (it could not be made: `review_error`)).

    What the warnings rest on comes from the effect itself, not the review, so a row whose review is missing or failed
    never looks safer than a reviewed one: the trees it brought (for `checked`) and every version it brought (for the
    trust note: `trusted` over all of them, `untrusted` the first LISTED_UNTRUSTED not trusted, `untrusted_count`
    them all) are read from its lock before and after.

    `checked` is judged on the trees the operation really brought (an entry whose tree did not change, one that only
    gained a local name, brought none) that the changeset still has (the last operation's lock): the named
    dependency's own tree when the operation brought it, else any tree it brought; an operation whose trees a later one
    replaced is not unchecked, it is `replaced_by` that later operation (checked null; that row says whether the
    package was checked). The trust note judges, for an open changeset, the project's trust as the adopt leaves it:
    a key the changeset takes out of project.json's `trusted` is not trusted (`trust_taken_back`)."""
    effs = [(op, i, e) for op in lc_ops for i, e in enumerate(op.get("effects") or []) if e.get("k") == "d"]
    final = None
    for op, i, e in effs:
        if e.get("state") == "installed" and isinstance(e.get("after"), dict):
            final = e["after"].get("lock")
    live_hashes = _lock_hashes(final)
    # the keys the changeset takes out of project.json's trusted: in its first operation's before, not in its last's
    # after (the adopt merges `trusted` as a set: a key the branch dropped goes)
    firsts = [e for _, _, e in effs if e.get("state") == "installed" and isinstance(e.get("before"), dict)]
    dropped = (set(_trusted_list(firsts[0]["before"].get("project"))) -
               set(_trusted_list((firsts[-1].get("after") or {}).get("project")))) if firsts else set()
    out = []
    for op, idx, e in effs:
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
            # the licence and size on the row are the named dependency's fetch's (by its package), the only fetch's
            # when there is one; else none, each fetch's being listed with it
            # the dependency's package: what the proposal recorded, else the lock's or project.json's entry for its
            # local name (an update names only that), else the request's, else the local name; its own fetch is the
            # one of that package (a fetch of another package is never its own)
            dep_pkg = e.get("package") or _package_of(e.get("name"), work_lock, work_project) or \
                (op.get("args") or {}).get("package") or e.get("name")
            # or, a direct git dependency's fetch carrying no package, the one of its repository (the URL as the
            # spec names it, which is what a fetch records) and subdir
            dep_git, dep_sub = _git_of(e, op, work_lock, work_project)
            mine_f = next((f for f in all_needs if f.get("package") == dep_pkg), None) or next(
                (f for f in all_needs if dep_git and _same_url(f.get("url"), dep_git)
                 and (f.get("subdir") or None) == (dep_sub or None)), None) or (
                all_needs[0] if len(all_needs) == 1 and not all_needs[0].get("package") else None)
            first = {k: data(mine_f.get(k)) for k in ("license", "size")} if mine_f else {}
            sizes = [f.get("size") for f in all_needs]
            row |= {"license": first.get("license"), "size": first.get("size"), "own": mine_f is not None,
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
        review, state = review_of(op, idx, e) if review_of is not None else (None, "pending")
        row["recorded"] = state == "stored"
        row["review_state"] = state if review is not None and not review.get("error") else (
            "failed" if review is not None else state)
        review = review or {}
        if review.get("error"):
            row["review_error"] = review["error"]
        pkgs = review.get("packages") or []
        # the licence and size on the row are the named dependency's own; when it did not move (only packages it
        # needs did), none is put on it: each package's are in its report, with the total
        mine = review["own"] if "own" in review else next(           # (a review from before `own` was kept)
            (p for p in pkgs if row["name"] in (p.get("names") or []) and p.get("do") != "del"), None)
        sizes = [p.get("size") for p in pkgs if p.get("do") != "del"]
        row |= {"packages": pkgs, "used_by": review.get("used_by") or [],
                "license": (mine or {}).get("license"), "size": (mine or {}).get("size"),
                "size_from": (mine or {}).get("size_from"), "own": mine is not None,
                "size_total": sum(sizes) if sizes and all(isinstance(x, int) for x in sizes)
                and not review.get("packages_more") else None,
                # copies: stored reviews may be shared, and the trust is the reader's to add
                "code": [dict(c) for c in review.get("code") or []]}
        for k in ("packages_more", "used_by_more", "code_more"):
            if review.get(k):
                row[k] = review[k]
        changes = changed(e.get("before") or {}, e.get("after") or {})
        brought = [(eb, names) for ea, eb, names in changes if eb is not None]
        # the keys the trust note judges: kept with the review when it read the tree (a store gc'd since names the
        # same ones), else read from the store now
        kept_keys = review.get("keys") or {}
        keys = list(dict.fromkeys(data(k) for eb, names in brought
                                  for k in (kept_keys.get(_entry_id(eb, names)) or entry_keys(names, eb))))
        # what the operation really brought (an entry that only gained a local name brought none) and the changeset
        # still has: for checked, the named dependency's own tree when it brought it, else any of them
        new = [(eb, names) for ea, eb, names in changes
               if eb is not None and (ea is None or _hash(ea) != _hash(eb)) and _hash(eb)]
        hashes = {_hash(eb) for eb, _ in new}
        own_h = {_hash(eb) for eb, names in new if row["name"] in names}
        here = (own_h or hashes) & live_hashes
        # per key, whether the changeset still has its tree (the last operation's lock): a key whose every tree a
        # later operation replaced is no longer in the changeset, whatever else the row brought
        live_key: dict[str, bool] = {}
        for eb, names in brought:
            for k in (kept_keys.get(_entry_id(eb, names)) or entry_keys(names, eb)):
                k = data(k)
                live_key[k] = live_key.get(k, False) or (_hash(eb) in live_hashes or not _hash(eb))
        # trust, per key: not trusted now (a run of its code is refused now), trusted now but taken back by this
        # changeset (a key it takes out of project.json's trusted: runs here still run it, the adopt takes it back), or
        # replaced (no longer in the changeset); an open changeset is judged as the adopt leaves it
        gone = dropped if is_open else set()
        trusted_after = {k for k in trusted_now if k not in gone}
        for c in row["code"]:
            ks = c.get("keys") or [c.get("key")]
            c["trusted"] = all(k in trusted_after for k in ks)
            c["trust"] = TRUST_WORDS[c["trusted"]]
            if not c["trusted"] and not all(live_key.get(k, True) for k in ks):
                c["replaced"] = True
                c["trust"] = TRUST_REPLACED
            elif not c["trusted"] and all(k in trusted_now for k in ks):
                c["taken_back"] = True
                c["trust"] = TRUST_CODE_TAKEN_BACK
        untrusted = [k for k in keys if k not in trusted_after]
        gone_keys = [k for k in untrusted if not live_key.get(k, True)]
        refused = [k for k in untrusted if k not in trusted_now and live_key.get(k, True)]
        back = [k for k in untrusted if k in trusted_now and live_key.get(k, True)]
        row["trusted"] = bool(keys) and not untrusted
        all_gone = bool(untrusted) and not refused and not back      # every version it names was replaced
        row["trust"] = (trust_words(refused, back, all_gone, gone_keys) if untrusted else TRUST_WORDS[True]) \
            if keys else None
        if keys:              # always there for an installed row: what the web tells apart, empty or not
            row["untrusted_now"] = refused[:LISTED_UNTRUSTED]
            row["untrusted_now_count"] = len(refused)
            row["trust_taken_back"] = back[:LISTED_UNTRUSTED]
            row["trust_replaced"] = gone_keys[:LISTED_UNTRUSTED]
        if untrusted:
            row["untrusted"] = untrusted[:LISTED_UNTRUSTED]
            row["untrusted_count"] = len(untrusted)
            if any(k.endswith(f"@{UNKNOWN_KEY}") for k in untrusted):
                row["trust_unknown"] = True
        before_t = _trusted_list((e.get("before") or {}).get("project"))
        after_t = set(_trusted_list((e.get("after") or {}).get("project")))
        row["trust_drop"] = [data(k) for k in before_t if k not in after_t]
        if row["action"] in ("remove", "pdk_set") or not hashes:
            row["checked"] = None                       # nothing came in that a check could read
        elif not here:
            # what it brought was replaced by a later operation: that operation's row is the one to check
            row["checked"] = None
            later = next((o.get("gseq") for o, _, x in effs if (o.get("gseq") or 0) > (op.get("gseq") or 0)
                          and x.get("state") == "installed" and x.get("name") == e.get("name")), None)
            row["replaced_by"] = later if later is not None else next(
                (o.get("gseq") for o, _, x in effs if (o.get("gseq") or 0) > (op.get("gseq") or 0)
                 and x.get("state") == "installed"), None)
        else:
            by = checks.find(here) if checks is not None else None
            row["checked"] = by is not None
            if by is not None:
                row["checked_by"] = by
        out.append(row)
    return out
