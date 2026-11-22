"""`python -m undine pkg ...`: packages from an index (docs/design/packages.md, phase 3; guide/libraries.md).

    pkg search QUERY            packages whose name or description holds QUERY
    pkg info PACKAGE            every version of a package, and what the newest says
    pkg add PROJECT PACKAGE     add it (PACKAGE@REQ for a requirement; --as for another local name)
    pkg update PROJECT [NAME]   move it (default: every package from an index) to the newest version it accepts
    pkg outdated PROJECT        locked versions against the newest accepted and the newest listed
    pkg tree PROJECT            the dependency tree as locked
    pkg why PROJECT PACKAGE     the chains from the project to a package
    pkg lock PROJECT            as `lib lock`: resolve what the lock does not pin
    pkg remove PROJECT NAME     as `lib remove`: a dependency, with the packages nothing needs any more
    pkg migrate PROJECT NAME    a git dependency becomes a package from an index, when the files are the same
    pkg sync PROJECT            as `lib sync`; --from-vendor DIR installs from a vendored bundle, offline
    pkg licenses PROJECT        the licence of every locked dependency and of the PDK
    pkg vendor PROJECT DIR      DIR/vendor/: the locked trees and their licences, for a tapeout bundle
    pkg index list | update     the indexes known here, and fetching them again
    pkg index add NAME URL      a nickname for an index, in ~/.undine/config.toml (--project: project.json)

Publishing (phase 5; LIBRARY is a project library's directory, or PROJECT/LIBRARY):

    pkg init LIBRARY            write LIBRARY/undine-package.toml from the library and its project, printing what
                                it guessed
    pkg pack LIBRARY            the index line of the release: the package's subdirectory clean, HEAD at its tag
                                (v<version>, or <name>-v<version> in a repository of several packages)

search, info and outdated read the local copy of each index (fetched once on first use); `pkg index update`, `pkg
update` and a resolution that finds no version it needs fetch it again."""
from __future__ import annotations

import argparse


def add_parser(sub) -> None:
    pk = sub.add_parser("pkg", help="packages from an index: search, add, update, tree").add_subparsers(
        dest="pkg_cmd", required=True)
    s = pk.add_parser("search", help="packages whose name or description holds QUERY")
    s.add_argument("query", nargs="?", default="")
    i = pk.add_parser("info", help="every version of a package")
    i.add_argument("package")
    for x in (s, i):
        x.add_argument("--index", help="an index nickname or URL (default: the default index)")
        x.add_argument("--project", help="a project, for the indexes its project.json names")
    a = pk.add_parser("add", help="add a package from an index to a project")
    a.add_argument("project")
    a.add_argument("package", help="PACKAGE or PACKAGE@REQ (e.g. analog_ip@^1.2; default: ^ the newest that fits)")
    a.add_argument("--as", dest="local", help="the local name its symbols are placed by (default: the package)")
    a.add_argument("--index", help="an index nickname or URL (default: the default index)")
    u = pk.add_parser("update", help="move packages to the newest version their requirement accepts")
    u.add_argument("project")
    u.add_argument("names", nargs="*", help="project.json names (default: every package from an index)")
    u.add_argument("--version", help="a new requirement for the one NAME given, written to project.json")
    for name, text in (("outdated", "locked versions against the newest"), ("tree", "the dependency tree as locked"),
                       ("lock", "resolve what the lock does not pin (as lib lock)")):
        x = pk.add_parser(name, help=text)
        x.add_argument("project")
        if name == "outdated":
            x.add_argument("--offline", action="store_true", help="read the local copies; fetch no index")
    rm = pk.add_parser("remove", help="remove a dependency and the packages nothing needs any more (as lib remove)")
    rm.add_argument("project")
    rm.add_argument("name")
    mg = pk.add_parser("migrate", help="turn a git dependency into a package from an index (same files)")
    mg.add_argument("project")
    mg.add_argument("name", help="the project.json name of the git dependency")
    mg.add_argument("--index", help="an index nickname or URL (default: the default index)")
    y = pk.add_parser("sync", help="install the dependencies exactly as locked (as lib sync)")
    y.add_argument("project")
    y.add_argument("--offline", action="store_true", help="fetch nothing; install from the store")
    y.add_argument("--from-vendor", metavar="DIR", help="read the trees `pkg vendor` wrote into DIR (a tapeout "
                   "bundle, or its vendor/) into the store first, then install offline")
    li = pk.add_parser("licenses", help="the licence of every locked dependency and of the PDK")
    li.add_argument("project")
    li.add_argument("--json", action="store_true", help="print LICENSES.json as pkg vendor writes it")
    v = pk.add_parser("vendor", help="write the locked trees and their licences into DIR/vendor/")
    v.add_argument("project")
    v.add_argument("dir")
    v.add_argument("--offline", action="store_true", help="fetch nothing: refuse a tree not in the store")
    v.add_argument("--yes", action="store_true", help="fetch what is not in the store without asking")
    w = pk.add_parser("why", help="the chains from the project to a package")
    w.add_argument("project")
    w.add_argument("package")
    lib_help = "the library's directory, or PROJECT/LIBRARY"
    ini = pk.add_parser("init", help="write undine-package.toml for a library of a project")
    ini.add_argument("library", help=lib_help)
    ini.add_argument("--name", help="the package name (default: the library's)")
    ini.add_argument("--version", help="the version (default: 0.1.0)")
    ini.add_argument("--license", help="an SPDX licence expression, e.g. Apache-2.0 (required to publish)")
    ini.add_argument("--repository", help="the git URL consumers fetch from (default: the remote origin)")
    ini.add_argument("--force", action="store_true", help="replace an existing undine-package.toml")
    pa = pk.add_parser("pack", help="print the index line of the tagged release")
    pa.add_argument("library", help=lib_help)
    pa.add_argument("--index", help="the index the line is for (default: the default index): a requirement from "
                    "another index gets its URL")
    pa.add_argument("--check", metavar="FILE", help="the check report (undine-check.json) the line links")
    pa.add_argument("--check-url", metavar="URL", help="the https URL the report is served at")
    ixp = pk.add_parser("index", help="the indexes known here").add_subparsers(dest="index_cmd", required=True)
    for name in ("list", "update", "add"):
        x = ixp.add_parser(name)
        if name == "update":
            x.add_argument("indexes", nargs="*", help="nicknames or URLs (default: every index known)")
        if name == "add":
            x.add_argument("name", help="the nickname (lower-case letters, digits, _ and -)")
            x.add_argument("url", help="the index's git URL")
        x.add_argument("--project", help="a project, for the indexes its project.json names" if name != "add"
                       else "write it into this project's project.json (a clone finds it) instead of config.toml")


def run(args: argparse.Namespace, store) -> int:
    from undine.pdks.pin import PinError
    from undine.store import cas, deps, gitsrc, pkgsource
    from undine.store import index as ix
    from undine.store import manifest as mf

    try:
        if args.pkg_cmd == "index":
            return _index(args, store)
        if args.pkg_cmd in ("init", "pack"):                    # a library, its project found from it
            src = pkgsource.locate(args.library, store)
            return {"init": _init, "pack": _pack}[args.pkg_cmd](src, args)
        if args.pkg_cmd in ("search", "info"):
            meta = store.get(args.project).meta if args.project else {}
            url = ix.url_of(args.index, meta)
            idx = ix.Index(url)
            return _search(idx, args.query, meta) if args.pkg_cmd == "search" else _info(idx, args.package, meta)
        prj = store.get(args.project)
        if args.pkg_cmd == "add":
            _add(prj, args)
        elif args.pkg_cmd == "update":
            _update(prj, args)
        elif args.pkg_cmd == "lock":
            from undine.__main__ import lib_command
            args.lib_cmd, args.format = "lock", None
            return lib_command(prj, args)
        elif args.pkg_cmd == "outdated":
            return _outdated(prj, args)
        elif args.pkg_cmd == "tree":
            return _tree(prj)
        elif args.pkg_cmd == "why":
            return _why(prj, args.package)
        elif args.pkg_cmd == "remove":
            from undine.__main__ import _remove
            _remove(prj, args)
            return 0
        elif args.pkg_cmd == "migrate":
            return _migrate(prj, args)
        elif args.pkg_cmd == "sync":
            return _sync(prj, args)
        elif args.pkg_cmd == "licenses":
            return _licenses(prj, args)
        elif args.pkg_cmd == "vendor":
            return _vendor(prj, args)
        st = deps.status(prj)
        for i in st["issues"]:
            print(f"! {i}")
        for w in st["warnings"]:
            print(f"~ {w}")
        return 0
    except (deps.DepError, ix.IndexReadError, PinError, OSError, pkgsource.PackageSourceError, mf.ManifestError,
            gitsrc.GitSourceError, cas.StoreError) as e:
        print(f"error: {e}")
        return 1


# ------------------------------------------------------------------ reading an index


def _search(idx, query: str, meta: dict) -> int:
    from undine.store import index as ix
    found = idx.search(query)
    if not found:
        print(f"no package in {ix.nickname(idx.url, meta)} holds {query!r}" if query else
              f"{ix.nickname(idx.url, meta)} lists no package")
        return 1
    width = max(len(x.label) for x in found)
    for x in found:
        desc = x.description or ""
        lic = f" ({x.license})" if x.license else ""
        print(f"{x.label:<{width}}  {desc}{lic}".rstrip())
    return 0


def _info(idx, package: str, meta: dict) -> int:
    from undine.store import index as ix
    vs = idx.versions(package)
    if not vs:
        print(f"error: {ix.nickname(idx.url, meta)} lists no package {package}: `pkg search` looks for a name, "
              "`pkg index update` fetches the index again")
        return 1
    live = [v for v in vs if not v.yanked] or vs
    top = live[0]
    print(f"{top.label} from {ix.nickname(idx.url, meta)}" + (f": {top.description}" if top.description else ""))
    print(f"  git      {top.git}" + (f" subdir {top.subdir}" if top.subdir else "") + f" @ {top.commit[:12]}")
    print(f"  tree     {top.tree}")
    if top.license:
        print(f"  license  {top.license}")
    if top.pdk:
        p = top.pdk
        print(f"  pdk      {p.get('family')} {', '.join(p.get('variants') or [])}"
              + (f", compat {', '.join(p['compat'])}" if p.get("compat") else ""))
    for d in top.deps:
        print(f"  needs    {d.text()}" + (f" from {ix.nickname(d.index, meta)}" if d.index else ""))
    if top.exports is not None:
        print(f"  exports  {', '.join(top.exports) or '(none)'}")
    print("  versions " + ", ".join(str(v.vers) + (" (yanked)" if v.yanked else "") for v in vs))
    return 0


def _index(args, store) -> int:
    from undine.store import index as ix
    meta = store.get(args.project).meta if args.project else {}
    known = ix.known(meta)
    if args.index_cmd == "list":
        for nick, url in known.items():
            i = ix.Index(url)
            c = i.commit()
            print(f"{nick:<12} {url}  " + (f"fetched at {c[:12]}" if c else "not fetched"))
        return 0
    if args.index_cmd == "add":
        return _index_add(args, store)
    urls = [ix.url_of(n, meta) for n in args.indexes] or list(dict.fromkeys(known.values()))
    bad = 0
    for url in urls:
        try:
            c = ix.Index(url).update()
            print(f"{ix.nickname(url, meta)}: {c[:12]}")
        except ix.IndexReadError as e:
            print(f"error: {e}")
            bad = 1
    return bad


# ------------------------------------------------------------------ changing a project


def _add(prj, args) -> None:
    from undine.__main__ import _package_line, _say_untrusted
    from undine.store import deps
    package, _, req = args.package.partition("@")
    local = args.local or package
    r = deps.add(prj, local, None, package=package, version=req or None, index=args.index)
    alias = f" as {local}" if local != package else ""
    print(f"{package} {r['version']}{alias} at {r['commit'][:12]} ({len(r['cells'])} cells), "
          f"requirement {prj.meta['libraries'][local].get('version')}, locked in {deps.LOCK}")
    brought = [x for x in r.get("resolved", []) if x["name"] != local and x.get("package")]
    for x in brought:
        print(_package_line(x))
    _say_untrusted(prj, [local, *(x["name"] for x in brought)])


def _update(prj, args) -> None:
    from undine.__main__ import _versions
    from undine.store import deps
    specs = prj.library_specs()
    names = args.names or [n for n, s in specs.items() if deps.is_index_spec(s)]
    if args.version and len(names) != 1:
        raise deps.DepError("--version is for one NAME")
    if not names:
        print("no package from an index in project.json")
        return
    for name in names:
        before = _versions(deps.read_lock(prj.path))
        r = deps.update(prj, name, version=args.version)
        after = _versions(deps.read_lock(prj.path))
        moved = {p for p in after if before.get(p) != after.get(p)} | {p for p in before if p not in after}
        if not moved:
            print(f"{name}: as locked")
        for p in sorted(moved):
            print(f"{p}: {before.get(p, '-')} -> {after.get(p, '-')}")
        for u in r["used_by"]:
            print(f"  used by  {u}")


def _outdated(prj, args) -> int:
    from undine.store import deps, resolve
    from undine.store import index as ix
    from undine.store.semver import Req, Version
    lock = deps.read_lock(prj.path) or {}
    g = resolve.lock_graph(lock)
    specs = prj.library_specs()
    rows, fetched = [], set()
    for id, e in sorted(g.entries.items()):
        url = e.get("index")
        if not isinstance(url, str):
            continue
        idx = ix.Index(url)
        if not args.offline and url not in fetched:
            idx.update()
            fetched.add(url)
        vs = [v for v in idx.versions(e["package"]) if not v.yanked]
        table, key = g.keys[id]
        req = Req.parse(specs[key].get("version", "*")) if table == "libraries" and key in specs else None
        have = Version.parse(e["version"])
        ok = next((v.vers for v in vs if req is None or req.matches(v.vers)), None)
        newest = vs[0].vers if vs else None
        if (ok is not None and ok > have) or (newest is not None and newest > have):
            rows.append((e["package"], str(have), str(ok or "-") if req else "-", str(newest or "-"),
                         "direct" if table == "libraries" else "via " + ", ".join(
                             g.entries[r].get("package") or g.keys[r][1] for r in g.requirers(id))))
    if not rows:
        print("every package from an index is at its newest version")
        return 0
    print(f"{'package':<20} {'locked':<10} {'accepted':<10} {'newest':<10}")
    for p, have, ok, newest, how in rows:
        print(f"{p:<20} {have:<10} {ok:<10} {newest:<10} {how}")
    print(f"`python -m undine pkg update {prj.name}` moves each to the newest its requirement accepts")
    return 0


def _label(e: dict, key: str) -> str:
    if e.get("package"):
        src = "" if isinstance(e.get("index"), str) else f" (git {str(e.get('commit') or '-')[:12]})"
        return f"{e['package']} {e.get('version') or '-'}{src}"
    return f"{key} (git {str(e.get('commit') or '-')[:12]})"


def _tree(prj) -> int:
    from undine.store import deps, resolve
    g = resolve.lock_graph(deps.read_lock(prj.path))
    print(prj.name)

    def walk(id: str, local: str, depth: int, seen: frozenset) -> None:
        e = g.entries[id]
        alias = f"{local} = " if e.get("package") and local != e.get("package") else ""
        again = " (*)" if id in seen else ""
        print(f"{'    ' * depth}{'└── '}{alias}{_label(e, g.keys[id][1])}{again}")
        if id in seen:
            return
        for n, t in sorted(g.deps.get(id, {}).items()):
            walk(t, n, depth + 1, seen | {id})
    for key, id in g.direct.items():
        walk(id, key, 0, frozenset())
    return 0


def _why(prj, package: str) -> int:
    from undine.store import deps, resolve
    g = resolve.lock_graph(deps.read_lock(prj.path))
    ids = [i for i, e in g.entries.items() if e.get("package") == package or g.keys[i][1] == package]
    if not ids:
        print(f"error: {deps.LOCK} locks no package {package}")
        return 1
    for id in ids:
        for chain in g.chains(id):
            print(g.chain_text(prj.name, chain))
    return 0


# ------------------------------------------------------------------ licences and vendoring


def _sync(prj, args) -> int:
    from undine.__main__ import _sync as lib_sync
    from undine.store import vendor
    if not args.from_vendor:
        lib_sync(prj, args)
        return 0
    r = vendor.sync_from_vendor(prj, args.from_vendor)
    for x in r["imported"]:
        print(f"{x['name']}: {x['action']}" + (f" from vendor/{x['dir']}" if x["dir"] else ""))
    for x in r["sync"]:
        print(f"{x['name']}: {x['action']} {(x['commit'] or '-')[:12]}")
        if x.get("warning"):
            print(f"~ {x['warning']}")
    return 0


def _licenses(prj, args) -> int:
    import json

    from undine.store import vendor
    data = vendor.licenses(prj)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    rows = [(r.get("package") or r["name"], r.get("version") or f"git {str(r.get('commit'))[:12]}",
             r.get("license") or "not recorded",
             r.get("license_from") or (", ".join(r["license_files"]) if r.get("license_files") else "-"),
             r.get("note")) for r in data["entries"]]
    if not rows:
        print("no dependency is locked")
    w = max([len(r[0]) for r in rows] + [10])
    for name, version, lic, where, note in rows:
        print(f"{name:<{w}}  {version:<16} {lic:<20} {where}" + (f"  ({note})" if note else ""))
    p = data["pdk"]
    if p:
        print(f"PDK {p.get('id')}: {p.get('license') or 'not recorded'}"
              + (f" ({p['license_from']})" if p.get("license_from") else ""))
    return 0


def _vendor(prj, args) -> int:
    from undine.store import vendor

    def confirm(names: list[str]) -> bool:
        print(f"not in the store or installed: {', '.join(names)}")
        if args.yes:
            return True
        try:
            return input("fetch them from their repositories? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False
    r = vendor.vendor(prj, args.dir, offline=args.offline, confirm=confirm)
    for x in r["entries"]:
        print(f"{x['dir']}: {x['files']} files, {x['bytes']} bytes")
    unknown = [e.get("package") or e["name"] for e in r["licenses"]["entries"] if not e.get("license")]
    print(f"wrote {r['dir']} ({len(r['entries'])} trees, vendor.json, LICENSES.json, LICENSES.md)")
    if unknown:
        print(f"~ no licence recorded for {', '.join(unknown)}: see their licence files in LICENSES.md")
    return 0


def _index_add(args, store) -> int:
    import json

    from undine.store import deps
    from undine.store import index as ix
    if not args.project:
        if ix.add_configured(args.name, args.url):
            print(f"{args.name} = {args.url} added to {ix.home() / 'config.toml'} [indexes]")
        else:
            print(f"{args.name} is {args.url} already")
        return 0
    prj = store.get(args.project)
    if not ix._NICK.match(args.name or ""):
        raise ix.IndexReadError(f"{args.name!r}: not a nickname (lower-case letters, digits, _ and -)")
    url = ix._check_url(args.url)
    with deps.meta_lock(prj.path):
        prj.meta = json.loads((prj.path / "project.json").read_text())     # as it is now, under the lock
        have = ix.project_indexes(prj.meta)
        if have.get(args.name) == url:
            print(f"{args.name} is {url} already in {prj.name}'s project.json")
            return 0
        if args.name in have:
            raise ix.IndexReadError(f"project.json: indexes.{args.name} is {have[args.name]} already: edit it to "
                                    "change it")
        prj.meta.setdefault("indexes", {})[args.name] = url
        prj.save()
    print(f"{args.name} = {url} added to {prj.name}'s project.json indexes")
    return 0


def _migrate(prj, args) -> int:
    from undine.pdks import pdkops
    from undine.store import deps
    lock = deps.read_lock(prj.path)
    if not lock or deps.lock_format(lock) < 2:
        deps.migrate(prj, args.name, index=args.index, dry=True)    # refused before the lock is converted
        up = pdkops.upgrade(prj)                         # the one way to format 2 (lib lock --format 2 does the same)
        print(f"{deps.LOCK}: format {up.get('format', 2)}")
    r = deps.migrate(prj, args.name, index=args.index)
    print(f"{args.name}: now {r['package']} {r['version']} from {r['index']}, requirement ={r['version']} "
          f"(`python -m undine pkg update {prj.name} {args.name} --version ^{r['version']}` loosens it)")
    if r["dropped_files"]:
        shown = ", ".join(r["dropped_files"][:10]) + (f" and {len(r['dropped_files']) - 10} more"
                                                      if len(r["dropped_files"]) > 10 else "")
        print(f"  no longer installed (outside the manifest's [files] include): {shown}")
    for u in r.get("used_by", []):
        print(f"  used by  {u}")
    return 0


# ------------------------------------------------------------------ publishing


def _init(src, args) -> int:
    from undine.store import manifest as mf
    from undine.store import pkgsource
    p = src.lib.path / mf.MANIFEST
    if p.is_symlink():
        raise pkgsource.PackageSourceError(f"{p} is a symbolic link: a manifest is a regular file; remove the link")
    if p.exists() and not args.force:
        raise pkgsource.PackageSourceError(f"{p} exists: edit it, or give --force to write a new one over it")
    d = pkgsource.draft(src, name=args.name, version=args.version, license=args.license,
                        repository=args.repository)
    p.write_text(mf.render(d.manifest, d.notes))
    print(f"wrote {p}")
    for key, value in d.guessed:
        print(f"  {key:<16} {value}")
    for n in d.notes:
        print(f"  # {n}")
    for w in d.warnings:
        print(f"~ {w}")
    return 0


def _pack(src, args) -> int:
    import hashlib

    from undine.store import index as ix
    from undine.store import pkgsource
    if bool(args.check) != bool(args.check_url):
        raise pkgsource.PackageSourceError("--check FILE and --check-url URL go together: the line links the "
                                           "report at the URL by the SHA-256 of the file")
    check = None
    if args.check:
        from pathlib import Path
        check = {"url": args.check_url, "sha256": hashlib.sha256(Path(args.check).read_bytes()).hexdigest()}
        if (why := ix.check_problem(check)) is not None:
            raise pkgsource.PackageSourceError(f"--check-url: check {why}")
    tag, commit = pkgsource.at_tag(src)
    url = ix.url_of(args.index, src.prj.meta)
    t = pkgsource.tree(str(src.top), commit, src.subdir, tag)
    print(ix.line_text(pkgsource.line_for(src, commit, t, url, check)))
    return 0
