"""A project library as the package it publishes (docs/design/packages.md, Publishing): where it is, in which git
repository, at which tag and commit, and the tree a consumer would install from it. `pkg init`, `pkg pack` and
`pkg check` start here.

    locate(arg, store)       the library a path (or PROJECT/LIBRARY) names, its project, repository and manifest
    tag_of(src)              v<version>, or <name>-v<version> when the repository holds more than one package
    at_tag(src)              the commit: the package's subdirectory clean, HEAD at that tag
    served(url, tag)         the commit the repository URL serves under the tag (git ls-remote)
    tree(url, commit, ...)   the include set at that commit, read from git's blobs into the store, and its hash
    placements(d)            the local names a library's schematics place, {local name: {cell}}
    project_pdk(prj)         the project's PDK: family, variant and full commit when one is known

Nothing here writes into the library or its repository; tree() writes into the store only, as a fetch does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from undine.store import cas, gitsrc
from undine.store import manifest as mf


class PackageSourceError(Exception):
    """A library that cannot be published as it is: not found, not in git, not at its tag, or a dependency."""


@dataclass
class Src:
    prj: object                         # store.project.Project
    lib: object                         # store.project.Library
    top: Path | None                    # the git repository's top, None outside git
    subdir: str | None                  # the library's directory below top, None at the top
    manifest: mf.Manifest | None

    @property
    def where(self) -> str:
        return f"{self.prj.name}/{self.lib.name}"


@dataclass
class Tree:
    hash: str
    files: dict = field(default_factory=dict)       # {path below subdir: (mode, object hex)}
    size: int = 0                                   # the files' bytes, summed per path


# ------------------------------------------------------------------ git in a working tree


def _git(top: Path, *args: str) -> str:
    return gitsrc._run(None, "-C", str(top), *args).decode("utf-8", "surrogateescape").strip()


def _git_top(d: Path) -> Path | None:
    try:
        out = _git(d, "rev-parse", "--show-toplevel")
    except gitsrc.GitSourceError:
        return None
    return Path(out) if out else None


def _rev(top: Path, ref: str) -> str | None:
    try:
        return _git(top, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}") or None
    except gitsrc.GitSourceError:
        return None


# ------------------------------------------------------------------ finding the library


def locate(arg: str, store, read_manifest: bool = True) -> Src:
    """The library `arg` names: a directory (a project library's, the project found by walking up to its
    project.json), or PROJECT/LIBRARY under the store's root. A dependency's or a PDK's library is refused: it is
    published from its own repository. Its manifest is read as publishing reads it (strict); read_manifest=False
    (`pkg init`, which may replace a manifest that does not read) leaves it None."""
    from undine.store.project import Project
    p = Path(arg).expanduser()
    if p.is_dir():
        d = p.resolve()
        up = next((x for x in (d, *d.parents) if (x / "project.json").is_file()), None)
        if up is None:
            raise PackageSourceError(f"{arg}: no project.json in it or above it: name a library directory of an "
                                     "Undine project, or PROJECT/LIBRARY")
        try:
            prj = Project(up, store.pdks)
        except ValueError as e:
            raise PackageSourceError(f"{up / 'project.json'}: cannot be read ({e})") from None
        libs = prj.libraries()
        lib = next((x for x in libs.values() if x.path.resolve() == d), None)
        if lib is None:
            own = ", ".join(n for n, x in libs.items() if x.origin == "project") or "none"
            raise PackageSourceError(f"{arg}: not a library of project {prj.name} (its own libraries: {own})")
    else:
        project, sep, name = arg.partition("/")
        if not sep or not name or "/" in name:
            raise PackageSourceError(f"{arg}: no such directory: name a library directory, or PROJECT/LIBRARY")
        try:
            prj = store.get(project)
        except (KeyError, ValueError) as e:
            raise PackageSourceError(f"{arg}: {e.args[0] if e.args else e}") from None
        lib = prj.libraries().get(name)
        if lib is None:
            raise PackageSourceError(f"{arg}: project {project} has no library {name}")
    if lib.origin != "project":
        what = "a dependency installed from elsewhere" if lib.origin == "dep" else "a PDK library"
        raise PackageSourceError(f"{prj.name}/{lib.name} is {what}: a package is published from its own library")
    top = _git_top(lib.path)
    subdir = None
    if top is not None:
        rel = lib.path.resolve().relative_to(top.resolve()).as_posix()
        subdir = None if rel == "." else rel
    m = mf.from_dir(lib.path, f"{prj.name}/{lib.name}", strict=True) if read_manifest else None
    return Src(prj, lib, top, subdir, m)


def need_manifest(src: Src) -> mf.Manifest:
    if src.manifest is None:
        raise PackageSourceError(f"{src.where}: no {mf.MANIFEST}: run `python -m undine pkg init {src.lib.path}` "
                                 "to write one")
    return src.manifest


def need_git(src: Src) -> Path:
    if src.top is None:
        raise PackageSourceError(f"{src.where}: {src.lib.path} is not in a git repository: commit the library to "
                                 "one and tag the release")
    return src.top


# ------------------------------------------------------------------ tag and commit


def tag_of(src: Src) -> str:
    """The release tag: v<version>, or <name>-v<version> when the repository's HEAD holds more than one manifest."""
    m, top = need_manifest(src), need_git(src)
    try:
        names = gitsrc._run(None, "-C", str(top), "ls-tree", "-r", "-z", "--name-only", "HEAD").split(b"\0")
    except gitsrc.GitSourceError:
        names = []                                  # no commit yet: at_tag says so
    n = sum(1 for x in names if x and x.rsplit(b"/", 1)[-1] == mf.MANIFEST.encode())
    return f"{m.name}-v{m.version}" if n > 1 else f"v{m.version}"


def dirty(src: Src) -> list[str]:
    """Paths under the package's subdirectory (from the repository top) with uncommitted changes, untracked files
    too; the rest of the repository is not looked at."""
    top = need_git(src)
    out = gitsrc._run(None, "-C", str(top), "--no-optional-locks", "status", "--porcelain=v1", "-z",
                      "--untracked-files=all", "--no-renames", "--", src.subdir or ".")
    return [e[3:].decode("utf-8", "surrogateescape") for e in out.split(b"\0") if len(e) > 3]


def at_tag(src: Src) -> tuple[str, str]:
    """(tag, commit): the package's subdirectory clean, and HEAD the commit its release tag names. Refused
    (PackageSourceError) otherwise, saying what to do."""
    need_manifest(src)
    top = need_git(src)
    tag = tag_of(src)
    head = _rev(top, "HEAD")
    if head is None:
        raise PackageSourceError(f"{src.where}: the repository has no commit yet: commit the library, then "
                                 f"`git tag {tag}`")
    changed = dirty(src)
    if changed:
        shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
        raise PackageSourceError(f"{src.where}: uncommitted changes under {src.subdir or 'the repository top'}: "
                                 f"{shown}: commit them (or remove them), then tag the commit {tag}")
    tagged = _rev(top, f"refs/tags/{tag}")
    if tagged is None:
        raise PackageSourceError(f"{src.where}: no tag {tag}: tag the release commit (`git tag {tag}`) and push "
                                 "the tag to the repository")
    if tagged != head:
        raise PackageSourceError(f"{src.where}: tag {tag} is {tagged[:12]}, HEAD is {head[:12]}: check out {tag}, "
                                 "or raise [package] version and tag HEAD with it")
    # The manifest read is the working copy (locate): it must be the committed one byte for byte. `git status` misses
    # an ignored file, and one marked skip-worktree or assume-unchanged, so the commit itself is read.
    rel = f"{src.subdir}/{mf.MANIFEST}" if src.subdir else mf.MANIFEST
    try:
        committed = gitsrc._run(None, "-C", str(top), "cat-file", "blob", f"{head}:{rel}")
    except gitsrc.GitSourceError:
        raise PackageSourceError(f"{src.where}: {rel} is not committed at {tag} (is it matched by .gitignore?): "
                                 "commit it (git add -f), then tag that commit") from None
    if committed != (src.lib.path / mf.MANIFEST).read_bytes():
        raise PackageSourceError(f"{src.where}: {rel} differs from the one committed at {tag}, though git status "
                                 "shows no change (skip-worktree or assume-unchanged?): `git update-index "
                                 f"--no-skip-worktree --no-assume-unchanged {rel}`, then commit it and tag that commit")
    return tag, head


def served(url: str, tag: str) -> str | None:
    """The commit `url` serves under refs/tags/<tag> (the tag's own commit when it is an annotated tag), None when
    it serves no such tag. Reads the remote with git ls-remote: the network for a remote URL."""
    gitsrc._check_url(url)
    try:
        out = gitsrc._run(None, "ls-remote", "--end-of-options", gitsrc._plain(url), f"refs/tags/{tag}",
                          f"refs/tags/{tag}^{{}}").decode("utf-8", "replace")
    except gitsrc.GitSourceError as e:
        raise PackageSourceError(f"{url}: cannot be read ({e})") from None
    refs = {}
    for row in out.splitlines():
        oid, _, ref = row.partition("\t")
        refs[ref.strip()] = oid.strip()
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def tree(url: str, commit: str, subdir: str | None, want: str) -> Tree:
    """The package's include set at `commit` of `url` (fetched into its kept repository as a dependency is, `want`
    the tag), read from git's blobs into the store and recorded: the pipeline deps._read_commit runs for a consumer,
    so this hash is the one a consumer computes."""
    with cas.store_lock(shared=True):
        gitsrc.fetch(url, None, want, commit)
        files = gitsrc.blob_tree(url, commit, subdir, "include")
        h = cas.write_tree(files)
    size = sum(cas.object_path(hx).stat().st_size for _, hx in files.values())
    return Tree(h, files, size)


def origin_url(top: Path) -> str | None:
    """The repository's `origin` remote URL, None when it has none."""
    try:
        return _git(top, "remote", "get-url", "origin") or None
    except gitsrc.GitSourceError:
        return None


# ------------------------------------------------------------------ what a library places and which PDK


def placements(d: Path) -> dict[str, dict[str, list[str]]]:
    """{local name: {cell: [schematics placing it]}} of the `C {X/cell.sym}` instances in the top-level .sch files of
    directory `d`. A symbol named without a library part is listed under ''."""
    from undine.schematics import xschem
    out: dict[str, dict[str, list[str]]] = {}
    for sch in sorted(Path(d).glob("*.sch")):
        try:
            comps = xschem.parse(sch.read_bytes().decode("utf-8", "replace")).comps
        except Exception:                        # a schematic that does not parse: pkg check's netlist step says so
            continue
        for c in comps:
            sym = (c.sym or "").strip()
            lib, sep, rest = sym.partition("/")
            if not sep:
                lib, rest = "", sym
            cell = rest[:-4] if rest.endswith(".sym") else rest
            out.setdefault(lib, {}).setdefault(cell, [])
            if sch.stem not in out[lib][cell]:
                out[lib][cell].append(sch.stem)
    return out


def project_pdk(prj) -> dict | None:
    """{family, variant, commit (full, or None when only a prefix is known), id} of the project's PDK: the installed
    PDK's, else undine.lock's format 2 pin, else project.json's id; None for a project without a PDK."""
    from undine.pdks.pdk import family_of
    from undine.store import deps

    def full(c) -> str | None:
        return c if isinstance(c, str) and mf._COMMIT.match(c) else None
    pdk = prj.pdk
    if pdk is not None and full(pdk.commit):
        return {"family": family_of(pdk.variant), "variant": pdk.variant, "commit": pdk.commit, "id": pdk.id}
    try:
        pin = deps.lock_pdk(deps.read_lock(prj.path) or {})
    except deps.DepError:
        pin = None
    if isinstance(pin, dict) and isinstance(pin.get("variant"), str) and full(pin.get("commit")):
        return {"family": family_of(pin["variant"]), "variant": pin["variant"], "commit": pin["commit"],
                "id": pin.get("id")}
    pid = prj.meta.get("pdk") or (pin or {}).get("id")
    if pdk is not None:
        return {"family": family_of(pdk.variant), "variant": pdk.variant, "commit": None, "id": pdk.id}
    if isinstance(pid, str) and pid:
        variant = pid.split("@", 1)[0]
        return {"family": family_of(variant), "variant": variant, "commit": None, "id": pid}
    return None


# ------------------------------------------------------------------ pkg init: a manifest drafted from the library


@dataclass
class Draft:
    manifest: mf.Manifest
    notes: list[str]                    # comment lines under [dependencies]: what could not be written as one
    guessed: list[tuple[str, str]]      # (key, what was written and from where), for the person to check
    warnings: list[str]


def draft(src: Src, *, name: str | None = None, version: str | None = None, license: str | None = None,
          repository: str | None = None) -> Draft:
    """The manifest `pkg init` writes for the library: name (the library's), version 0.1.0, the cells with a symbol
    exported, the design's include list, [pdk] from the project's PDK (built_with and commits its full commit), the
    project's index dependencies that the library's schematics place (^ their locked version), repository from
    the git remote `origin`. What it could not write (a placed git dependency, a library of the project) becomes a
    comment line. Refused for a name that is not a package name or a licence that is not an SPDX expression."""
    from undine.store import deps, spdx
    from undine.store import index as ix
    from undine.store.semver import SemverError, Version
    prj, lib = src.prj, src.lib
    guessed: list[tuple[str, str]] = []
    warnings: list[str] = []
    pname = name or lib.name
    if not mf.NAME.match(pname):
        raise PackageSourceError(f"{pname!r} is not a package name ({mf._NAME_FIX}): give one with --name")
    if name is None:
        guessed.append(("name", f"{pname} (the library's name)"))
    try:
        vers = Version.parse(version or "0.1.0")
    except SemverError as e:
        raise PackageSourceError(f"--version: {e}") from None
    if version is None:
        guessed.append(("version", f"{vers}"))
    if license is not None and (why := spdx.problem(license)):
        raise PackageSourceError(f"--license: {why}")
    if license is None:
        warnings.append('no license: publishing needs one; add license = "<SPDX expression>" to [package] '
                        "(e.g. Apache-2.0)")
    if repository is None and src.top is not None:
        repository = origin_url(src.top)
        if repository is not None:
            guessed.append(("repository", f"{repository} (git remote origin)"))
    if repository is None:
        warnings.append('no repository: publishing needs one; add repository = "<the git URL consumers fetch>"')
    exports = tuple(sorted(p.stem for p in lib.path.glob("*.sym")))
    guessed.append(("exports.cells", f"{', '.join(exports) or '(none)'} (the cells that have a symbol)"))
    guessed.append(("files.include", ", ".join(mf.DEFAULT_INCLUDE)))
    pdk = None
    pp = project_pdk(prj)
    if pp is None:
        warnings.append("the project has no PDK: no [pdk] written; a package's layouts are for one PDK: add it")
    else:
        commit = pp["commit"]
        pdk = mf.PdkReq(pp["family"], (pp["variant"],), (commit,) if commit else (), (), commit)
        guessed.append(("pdk", f"{pp['family']} {pp['variant']}" + (f", built_with and commits {commit}" if commit
                                                                     else " (the project's PDK)")))
        if commit is None:
            warnings.append(f"the project's PDK {pp['id']} has no full commit here: no built_with written; "
                            "`python -m undine pdk verify PROJECT --pin` records it, then run pkg init --force")
    specs = prj.library_specs()
    try:
        locked = (deps.read_lock(prj.path) or {}).get("libraries") or {}
    except deps.DepError as e:
        raise PackageSourceError(str(e)) from None
    libs = prj.libraries()
    pdk_libs = {n for n, x in libs.items() if x.origin == "pdk"} | {"devices"}
    dependencies: dict = {}
    notes: list[str] = []
    for local, cells in sorted(placements(lib.path).items()):
        if local in ("", pname) or local in pdk_libs:
            continue
        shown = ", ".join(sorted(cells)[:3]) + (" ..." if len(cells) > 3 else "")
        if local == lib.name:
            warnings.append(f"the schematics place the library's own cells as {local}/... ({shown}): a consumer "
                            f"installs the package as {pname}; name the package {local}, or place them as {pname}/...")
            continue
        spec = specs.get(local)
        if spec is not None and "git" in spec:
            notes.append(f"{local}: a git dependency ({spec['git']} @ {spec.get('rev') or 'HEAD'}): a published "
                         "package depends only on packages from an index: publish it to one, then add it here")
        elif spec is not None and deps.is_index_spec(spec):
            e = locked.get(local) if isinstance(locked.get(local), dict) else {}
            v = e.get("version")
            req = f"^{v}" if isinstance(v, str) else spec.get("version") or "*"
            url = ix.url_of(spec["index"], prj.meta) if spec.get("index") else None
            dependencies[local] = mf.IndexDep(local, spec.get("package") or local, req, url)
            guessed.append((f"dependencies.{local}", f"{spec.get('package') or local} {req}"
                            + (f" from {url}" if url else "") + f" (placed: {shown})"))
        elif spec is not None:
            notes.append(f"{local}: a library of this project, not a package: publish it as one, then add it here")
        elif local in libs and libs[local].origin == "dep":
            notes.append(f"{local}: installed because another package needs it: add it here if this package "
                         "places it on purpose")
        else:
            notes.append(f"{local}: placed ({shown}) but neither a dependency of the project nor a PDK library")
    m = mf.Manifest(name=pname, version=vers, license=license, repository=repository, pdk=pdk,
                    dependencies=dependencies, exports=exports, include=mf.DEFAULT_INCLUDE)
    text = mf.render(m, notes)
    try:
        back = mf.parse(text.encode(), src.where, strict=True)
    except mf.ManifestError as e:
        raise PackageSourceError(f"the manifest drafted does not read back ({e}): write it by hand") from None
    return Draft(back, notes, guessed, warnings)


# ------------------------------------------------------------------ pkg pack: the index line


def line_for(src: Src, commit: str, t: Tree, index_url: str, check: dict | None = None):
    """The index line of the package at `commit` with tree `t`, for the index at `index_url`: git is the manifest's
    repository; a requirement's index is written as its URL when it is not this index (cargo's rule). Refused for
    a git dependency (a published package depends only on index packages) and a manifest without repository."""
    from undine.store import index as ix
    from undine.store.semver import Req, SemverError
    m = need_manifest(src)
    if not m.repository:
        raise PackageSourceError(f"{src.where}: {mf.MANIFEST}: package.repository: missing: add the git URL "
                                 "consumers fetch the package from")
    out = []
    for local, d in m.dependencies.items():
        if isinstance(d, mf.GitDep):
            raise PackageSourceError(f"{src.where}: {mf.MANIFEST}: dependencies.{local}: a git dependency: a "
                                     "published package depends only on packages from an index")
        url = ix.url_of(d.index, src.prj.meta) if d.index else None
        try:
            req = Req.parse(d.version)
        except SemverError as e:
            raise PackageSourceError(f"{src.where}: {mf.MANIFEST}: dependencies.{local}.version: {e}") from None
        out.append(ix.Dep(local, d.package, req, None if url == index_url else url))
    pdk = None
    if m.pdk is not None:
        p = m.pdk
        pdk = {"family": p.family, "variants": list(p.variants)}
        pdk |= {k: list(v) for k, v in (("commits", p.commits), ("compat", p.compat)) if v}
        if p.built_with:
            pdk["built_with"] = p.built_with
    return ix.Line(m.name, m.version, m.repository, commit, t.hash, index_url, src.subdir, pdk, tuple(out),
                   m.exports, m.license, t.size, check, False, m.description)
