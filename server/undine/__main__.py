"""Undine command line.

    python -m undine serve   [--root DIR] [--port 8700]
    python -m undine pdk list                    the PDKs found, with commit, source and libraries (also: pdks)
    python -m undine pdk verify PROJECT [--pin] [--network]   the installed PDK against undine.lock's pin; --pin pins it
                                                 as installed (writes lock format 2); --network: ciel may resolve a
                                                 commit prefix
    python -m undine pdk fetch PROJECT [--yes] [--network]    install the project's PDK with ciel (asks first)
    python -m undine doctor                      check prerequisites, print the fix for each missing one
    python -m undine new     PROJECT [--pdk sky130A]
    python -m undine set-pdk PROJECT PDK         pin the project to another PDK version (e.g. sky130A@8afc8346a57f);
                                                 refused, listing them, when locked packages do not accept it
    python -m undine import  PROJECT LIB FILE.gds|FILE.oas
    python -m undine lib add PROJECT NAME GIT [--rev REV] [--subdir DIR] [--package P]   a library dependency,
                                                     pinned in undine.lock with the packages its manifest needs
                                                     (--package: the [package] name the repository must have)
    python -m undine lib update PROJECT NAME [--rev REV]    move it to another version; prints what changed, its
                                                     packages added, moved and removed too
    python -m undine lib remove PROJECT NAME         with the packages only it needed; says which names stay placed
    python -m undine lib trust PROJECT NAME [--untrust] [--yes]   lists what a simulation would refuse in the
                                                     installed dependency, asks, and trusts its version (a person's
                                                     decision: no terminal and no --yes, refused); --untrust takes
                                                     that back
    python -m undine lib sync PROJECT [--offline]    install the dependencies exactly as locked (--offline, or
                                                     UNDINE_OFFLINE=1: from the store only, never fetched)
    python -m undine lib lock PROJECT [--format 2]   resolve what the lock does not pin, record the PDK and tool
                                                     versions in use now; --format 2: write lock format 2 (in one
                                                     write with what it resolves, when a package needs format 2)
    python -m undine lib status PROJECT              what differs from undine.lock here, a row per local name with
                                                     its package, what places it and whether it is trusted (exit 1
                                                     on an issue; another tool version is a warning, ~)
    python -m undine store verify [--dry-run]    re-hash the store's files (~/.undine/store, $UNDINE_STORE);
                                                 remove those that do not match (exit 1 on any problem)
    python -m undine store gc [--dry-run]        remove what no project here locks or installs (the projects under
                                                 --root, their changesets, and the ones that synced elsewhere)
    python -m undine git-setup PROJECT           make the project a git repository that diffs layout cells as text
                                                 and merges a cell's files object by object
    python -m undine textconv FILE.oas           a layout file as text (what git-setup registers for git diff)
    python -m undine merge-driver [--virtual-base] BASE OURS THEIRS PATH   the merge driver git-setup registers (git
                                                 runs it; --virtual-base: for the merge of a criss-cross merge's bases)
    python -m undine resolve PATH [--theirs ID ...] [--ours ID ...] [--all-theirs | --all-ours]
                                                 merge a conflicted file again, taking a side for those conflicts
    python -m undine merge-check [DIR] [--commit C] [--json]   open conflicts, warnings and the cells to verify
    python -m undine precommit                   the pre-commit hook git-setup installs: refuses unsaved layout edits
    python -m undine diff OLD NEW [--kind K] [--json]          semantic diff of two files
    python -m undine diff --rev A [B] [--dir DIR] [--json]     ... of a project's files between two revisions
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Callable

from undine.pdks.pdk import default_pdk_roots, discover
from undine.store.project import ProjectStore, import_layout

DEFAULT_ROOT = Path(os.environ.get("UNDINE_ROOT", Path.home() / "undine-projects"))


MERGED = ["*.sch", "*.sym", "*.oas", "*.netlist.yaml", "*.intent.yaml", "history/*.jsonl"]   # vcs.gitmerge.kind_of
GITATTRIBUTES = ["*.oas diff=undine-layout", "*.gds diff=undine-layout", *(f"{p} merge=undine" for p in MERGED)]
# Python 3.10 has no -P: -c puts "" (the current directory) first on the path, the prelude takes it out
PRELUDE = ('import sys; del sys.path[0]; import runpy; '
           'runpy.run_module("undine", run_name="__main__", alter_sys=True)')
HOOK_MARK = "# undine git-setup: refuses a commit whose staged operation logs hold unsaved layout edits"


def undine_command(*args: str, version: tuple[int, ...] | None = None) -> str:
    """The shell command (as git runs a driver or a hook) of `python -m undine ARGS` with this Python. git runs it in
    the repository's top directory, which `-m` puts first on the module path: a committed yaml.py (or undine/) would
    run instead. -P leaves it out; before Python 3.11, PRELUDE does (-I would too, but also drops the user's
    site-packages and PYTHONPATH, where Undine may be installed). ARGS go in as they are (git's %O ...)."""
    py = shlex.quote(sys.executable)
    if (version or sys.version_info) >= (3, 11):
        return " ".join([py, "-P", "-m", "undine", *args])
    return " ".join([py, "-c", shlex.quote(PRELUDE), *args])


def _add_lines(path: Path, lines: list[str]) -> list[str]:
    """Appends the `lines` the file does not have (every other line kept as it is); returns those."""
    have = path.read_text().splitlines() if path.is_file() else []
    new = [x for x in lines if x not in have]
    if new:
        path.write_text("\n".join(have + new) + "\n")
    return new


_WORD = r"""(?:[^\s'"\\;&|<>()]|\\.|'[^']*'|"(?:[^"\\]|\\.)*")+"""     # a shell word, quoted parts and all
# the command of a line that runs `undine precommit`: <python> [-P ...] -m undine precommit, <python> -c PRELUDE
# precommit (Python 3.10), or an `undine` script
_RUNS_PRECOMMIT = re.compile(rf"{_WORD}(?:\s+-\w+)*\s+(?:-m\s+undine|-c\s+{_WORD})\s+precommit\b"
                             r"|(?<![^\s;&|(])(?:\S*/)?undine\s+precommit\b")


def _hook(old: str | None, run: str) -> str:
    """The pre-commit hook's text: a new one, or `old` (ours, HOOK_MARK in it, maybe with lines the user added) with
    only the command of the line that runs `undine precommit` replaced (the Python may have moved: what the line has
    around it, such as `exec`, stays), every other line kept. When no line runs it any more, it goes right after the
    mark, without exec, so that the lines after it still run."""
    if old is None:
        return f"#!/bin/sh\n{HOOK_MARK}\nexec {run}\n"
    lines = old.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("#") and (m := _RUNS_PRECOMMIT.search(line)):
            lines[i] = line[:m.start()] + run + line[m.end():]
            return "".join(lines)
    k = next(i for i, line in enumerate(lines) if HOOK_MARK in line)
    lines[k] = lines[k].rstrip("\n") + "\n"
    lines.insert(k + 1, f"{run} || exit 1\n")
    return "".join(lines)


def _install_hook(d: Path, git) -> str | None:
    """Installs the pre-commit hook (`undine precommit`) unless the repository has a hook of its own. HOOK_MARK tells
    ours, whose line that runs `undine precommit` is replaced (the Python may have moved) and whose other lines (the
    user's) are kept. None when installed, else why not."""
    run = undine_command("precommit")
    hooks = subprocess.run(["git", "-C", str(d), "config", "core.hooksPath"],     # exits 1 when it is not set
                           capture_output=True, text=True).stdout.strip()
    if hooks:                                       # shared or managed hooks: not ours to write into
        return f"the pre-commit hook is not installed (core.hooksPath is {hooks}): add `{run}` to its pre-commit"
    path = (d / git("rev-parse", "--git-common-dir")).resolve() / "hooks" / "pre-commit"
    old = path.read_text(errors="replace") if path.exists() else None
    if old is not None and HOOK_MARK not in old:
        return f"the pre-commit hook is not installed ({path} is not Undine's): add `{run}` to it"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_hook(old, run))
    path.chmod(0o755)
    return None


def git_setup(d: Path, say: Callable[[str], object] | None = print) -> dict:
    """Makes a project directory a git repository (if it is not in one): job results, tapeout bundles and the
    assistant's log are ignored, `git diff` shows layout cells as sorted text (textconv), `git merge` merges the files
    of a cell (schematic, symbol, layout, netlist, intent) and the operation log object by object (the merge driver,
    undine.vcs.gitmerge; for the virtual merge base of a criss-cross merge, the one merge.undine.recursive names, which
    takes the bases' own base where they conflict, so that the merge reports the conflict), and a pre-commit hook
    refuses a commit whose staged operation logs hold unsaved layout edits (`undine precommit`). Running it again on a
    project set up before adds what is missing and replaces the commands git runs (in the hook, only the line that runs
    `undine precommit`). Nothing is committed; a repository with no commit yet is said to have none (an applied
    changeset is committed only onto a branch with a commit: Undine's own callers make the first commit,
    branches.first_commit). Returns what it did, {"init": it ran git init, "hook": None when the hook is installed,
    else why not, "gitignore": the lines it added to .gitignore}, and tells it to `say` (the command line's lines;
    None: quiet, the HTTP API)."""
    def git(*a: str) -> str:
        return subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True, text=True).stdout.strip()
    from undine.vcs.gitmerge import GITIGNORE      # Undine's own output, which the history never lists either

    say = say or (lambda _: None)
    init = False
    try:
        git("rev-parse", "--git-dir")
    except subprocess.CalledProcessError:
        git("init", "-q")
        init = True
        say(f"git init {d}")
    _add_lines(d / ".gitattributes", GITATTRIBUTES)
    ignored = _add_lines(d / ".gitignore", GITIGNORE)
    if ignored and not init:
        say(f"{d}/.gitignore: added {', '.join(ignored)} (Undine's own output)")
    for key, value in (("diff.undine-layout.textconv", undine_command("textconv")),
                       ("diff.undine-layout.cachetextconv", "true"),
                       ("merge.undine.name", "Undine: cells and the operation log, object by object"),
                       ("merge.undine.driver", undine_command("merge-driver", "%O", "%A", "%B", "%P")),
                       # the merge of a criss-cross merge's several bases: their own base where they conflict
                       ("merge.undine.recursive", "undine-base"),
                       ("merge.undine-base.name", "Undine: the merge of several merge bases (their base where they "
                                                  "conflict)"),
                       ("merge.undine-base.driver",
                        undine_command("merge-driver", "--virtual-base", "%O", "%A", "%B", "%P"))):
        git("config", "--replace-all", key, value)      # a project set up before: its old values, however many
    not_hooked = _install_hook(d, git)
    done = [".gitattributes", ".gitignore", "the layout diff driver", "the merge driver"]
    done += [] if not_hooked else ["the pre-commit hook"]
    say(f"{d}: {', '.join(done[:-1])} and {done[-1]} are set up (git diff shows cells as text, git merge merges "
        "them object by object; after a merge: undine merge-check)" + (f"; {not_hooked}" if not_hooked else ""))
    if subprocess.run(["git", "-C", str(d), "rev-parse", "-q", "--verify", "HEAD"], capture_output=True).returncode:
        say(f"{d}: no commit yet: commit the project's files with git, or with Make the first commit in Undine's "
            "History panel (a changeset applied before that is left uncommitted)")
    return {"init": init, "hook": not_hooked, "gitignore": ignored}


def vcs_command(args) -> int:
    """merge-driver, resolve, merge-check, precommit, diff: the semantic merge and diff with git
    (undine.vcs.gitmerge)."""
    import json

    from undine.vcs import gitmerge

    if args.cmd == "merge-driver":
        return gitmerge.driver(args.base, args.ours, args.theirs, args.path, virtual_base=args.virtual_base)
    if args.cmd == "resolve":
        rel = gitmerge.locate(args.path)                # from here or from the top (the driver prints the latter)
        left = gitmerge.resolve(f":/{rel}", args.theirs, args.ours, args.all_side)
        if left:
            print(f"{rel}: {len(left)} conflict{'s' * (len(left) != 1)} open (ours in the file):")
            for c in left:
                print(f"  {c.id}  {c.what}")
            return 1
        print(f"{rel}: no conflict open; `git add {shlex.quote(':/' + rel)}` marks it resolved")   # from anywhere
        return 0
    if args.cmd == "merge-check":
        r = gitmerge.merge_check(args.dir, args.commit)
        print(json.dumps(r, indent=1) if args.json else gitmerge.format_check(r))
        return 1 if r["unmerged"] else 0
    if args.cmd == "precommit":                     # the pre-commit hook: git shows what it prints
        logs = gitmerge.precommit()
        for x in logs:
            print(f"undine: {x['path']}: library {x['lib']} has layout edits not saved to its cell files"
                  + (f" (cells {', '.join(x['cells'])})" if x["cells"] else ""), file=sys.stderr)
        if logs:
            print("undine: save the layout in Undine, `git add` the cell files and the log, and commit again "
                  "(git commit --no-verify commits the unsaved edits as a journal in the log: a merge with a branch "
                  "that changed the layout too then stops on it)", file=sys.stderr)
        return 1 if logs else 0
    if args.rev:                                    # diff
        if args.files or len(args.rev) > 2:
            raise ValueError("diff --rev A [B]: one or two revisions, no files")
        files = gitmerge.diff_revs(args.dir, *args.rev)
    elif len(args.files) == 2:
        files = {str(args.files[1]): gitmerge.diff_files(args.files[0], args.files[1], args.kind)}
    else:
        raise ValueError("diff OLD NEW, or diff --rev A [B]")
    print(json.dumps(files, indent=1) if args.json else gitmerge.format_diff(files) or "no change")
    return 0


def _print_warnings(st: dict, notes: bool) -> None:
    """A status's warnings (another tool version or origin: worth a look, never a lock issue) and its notes."""
    for w in st.get("warnings", []):
        print(f"~ {w}")
    for n in st.get("notes", []) if notes else []:
        print(f"  note: {n}")


def _say_created(project_path: Path) -> None:
    """The line every command that made undine.lock prints, with the format it got."""
    from undine.store import deps

    lock = deps.read_lock(project_path) or {}
    print(f"created {deps.LOCK} (format {deps.lock_format(lock)})")


def _entry_text(e: dict) -> str:
    """A lock entry in one line: its commit and the hash that pins its files."""
    pinned = f"tree {e['tree'][:19]}" if e.get("tree") else f"sha {(e.get('sha') or '-')[:12]}"
    return f"{(e.get('commit') or '-')[:12]} ({pinned})"


def _and(items) -> str:
    items = [str(x) for x in items]
    return " and ".join(items) if len(items) < 3 else ", ".join(items[:-1]) + " and " + items[-1]


def _package_line(x: dict) -> str:
    """A package brought in by add, update or lib lock: '+ sar_common 1.3.1 as sar_common, via analog_ip'."""
    via = f", via {', '.join(x['via'])}" if x.get("via") else ""
    return f"  + {x['package']} {x.get('version') or '-'} as {', '.join(x.get('names') or [x['name']])}{via}"


def _versions(lock: dict | None) -> dict[str, str]:
    """{package: version} of every entry of a lock that names its package."""
    out = {}
    for table in ("libraries", "packages"):
        for e in ((lock or {}).get(table) or {}).values():
            if isinstance(e, dict) and isinstance(e.get("package"), str):
                out[e["package"]] = str(e.get("version") or "-")
    return out


def _say_untrusted(prj, names) -> None:
    """A '~' line per dependency installed under one of `names` (each install once) that holds what the trust rule
    refuses and that the project does not trust: a simulation using it is refused until a person runs lib trust."""
    from undine.store import deps, trust

    trusted, said = trust.trusted_keys(prj.path), set()
    for name in names:
        d = deps.dep_dir(prj.path, name)
        if not d.is_dir():
            continue
        key = trust.key_of(d)
        if key in trusted or key in said:
            continue
        said.add(key)
        found = trust.scan_dir(d)
        if any(found.values()):
            _, label, _ = trust._identity(d)
            print(f"~ {label}: {deps._flag_text(found)} (not trusted): a simulation using it needs "
                  f"`python -m undine lib trust {prj.name} {name}`")


def _add(prj, args) -> None:
    from undine.store import deps

    r = deps.add(prj, args.name, args.git, args.rev, args.subdir, package=args.package)
    if r.get("package"):
        internal = f", {len(r['internal_cells'])} internal" if r.get("internal_cells") else ""
        print(f"{args.name}: {r['package']} {r['version']} at {r['commit'][:12]} ({len(r['cells'])} cells{internal}), "
              f"locked in {deps.LOCK}")
        if len(r.get("names") or []) > 1:                   # an alias: also under the package's own name
            print(f"  installed as {_and(r['names'])}")
    else:
        print(f"{args.name}: {r['commit'][:12]} ({len(r['cells'])} cells), locked in {deps.LOCK}")
    brought = [x for x in r.get("resolved", []) if x["name"] != args.name and x.get("package")]
    for x in brought:
        print(_package_line(x))
    _say_untrusted(prj, [args.name, *(x["name"] for x in brought)])


def _update(prj, args) -> None:
    from undine.store import deps

    before = _versions(deps.read_lock(prj.path))           # for the versions the moved packages had
    r = deps.update(prj, args.name, args.rev)
    after = _versions(deps.read_lock(prj.path))
    print(f"{args.name}: {(r['from'] or '-')[:12]} -> {r['to'][:12]}")
    for k in ("added", "modified", "removed"):
        for f in r["changed"][k]:
            print(f"  {k:8} {f}")
    others = [p for p in r.get("packages_changed", []) if args.name not in p["names"]]
    for p in others:                                        # the packages it brought in, moved and no longer needs
        label = p["package"] or p["names"][0]
        if p["from"] is None:
            print(f"  + {label} {after.get(p['package'], '-')} as {', '.join(p['names'])}")
        elif p["to"] is None:
            print(f"  - {label} {before.get(p['package'], '-')} as {', '.join(p['names'])}: no longer needed")
        else:
            print(f"  {label}: {before.get(p['package'], '-')} @ {p['from'][:12]} -> {after.get(p['package'], '-')} @ "
                  f"{p['to'][:12]}")
    for u in r["used_by"]:
        print(f"  used by  {u}")
    _say_untrusted(prj, [args.name, *(p["names"][0] for p in others if p["to"] is not None)])


def _remove(prj, args) -> None:
    from undine.store import deps

    r = deps.remove(prj, args.name)
    for x in r.get("removed_packages", []):
        if x.get("package"):
            print(f"removed {x['package']} {x.get('version') or '-'} (installed as {', '.join(x['names'])})")
        else:
            print(f"removed {', '.join(x['names'])} (git {(x.get('commit') or '-')[:12]})")
    for k in r.get("kept", []):
        print(f"{k['name']}: still placed by {_and(k['placed_by'])}")


def _sync(prj, args) -> None:
    from undine.store import deps

    # None: UNDINE_OFFLINE decides, as for the server
    for r in deps.sync(prj, offline=True if args.offline else None):
        print(f"{r['name']}: {r['action']} {(r['commit'] or '-')[:12]}")      # removed: maybe no commit
        if r.get("warning"):                                # installed as locked all the same
            print(f"~ {r['warning']}")


def _row_text(lib: dict) -> str:
    """A dependency row's package part: its package and version ('-> package' under another local name) and whether
    the project trusts it."""
    parts = []
    if lib.get("package"):
        alias = "-> " if lib["package"] != lib["name"] else ""
        parts.append(f"{alias}{lib['package']} {lib.get('version') or '-'}")
    if lib.get("trusted") is not None:
        parts.append("trusted" if lib["trusted"] else "not trusted")
    return "  " + ", ".join(parts) if parts else ""


def _print_status(st: dict, said: list[str]) -> None:
    """The status listing; `said`: issues the command printed already (as '!' lines), not printed again."""
    pdk = st["pdk"]
    print(f"  {'PDK':<20} {'pdk':<10} {pdk['state']:<14} {pdk['locked'] or pdk['requested'] or '-'}")
    for lib in st["libraries"]:
        where = lib.get("git") or lib.get("path") or ""
        dep = lib["kind"] == "dependency"
        ver = f" {lib.get('rev') or 'HEAD'} @ {(lib.get('locked') or '-')[:12]}" if dep else ""
        print(f"  {lib['name']:<20} {lib['kind']:<10} {lib['state']:<14} {where}{ver}{_row_text(lib) if dep else ''}")
        if dep and lib.get("direct") is False and lib.get("state") != "leftover":
            if lib.get("via"):                          # placed by packages: by which
                print(f"  {'':<20} via {lib.get('chain') or ', '.join(lib['via'])}")
            elif others := [n for n in lib.get("names") or [] if n != lib["name"]]:
                print(f"  {'':<20} = {', '.join(others)}")   # a direct package's own name: the same install
    for i in st["issues"]:
        if i not in said:
            print(f"! {i}")
    _print_warnings(st, notes=True)
    if not st["issues"]:
        print("as locked")


def lib_command(prj, args) -> int:
    from undine.store import deps

    had_lock = (prj.path / deps.LOCK).exists()
    said: list[str] = []
    try:
        if args.lib_cmd == "trust":
            return _trust(prj, args)
        if args.lib_cmd == "add":
            _add(prj, args)
        elif args.lib_cmd == "update":
            _update(prj, args)
        elif args.lib_cmd == "remove":
            _remove(prj, args)
        elif args.lib_cmd == "sync":
            _sync(prj, args)
        elif args.lib_cmd == "lock":
            said = _lock(prj, args)
        if not had_lock and (prj.path / deps.LOCK).exists():
            _say_created(prj.path)
        st = deps.status(prj)
        if args.lib_cmd == "status" or st["issues"]:
            _print_status(st, said)
        else:                                       # after a change: what differs from the lock, not the full listing
            _print_warnings(st, notes=False)
        return 0 if st["ok"] or args.lib_cmd not in ("status",) else 1        # issues only: a warning never fails
    except deps.DepError as e:                      # ResolveError, NeedsFormat2, NotDirect: every line of it
        print(f"error: {e}")
        return 1


def _trust(prj, args) -> int:
    """`lib trust`: what the trust rule refuses in the installed dependency, listed for a person who then trusts its
    version (its key) in the project; --untrust takes that back without asking. Asks on a terminal; with neither a
    terminal nor --yes it is refused, so that nothing without a person at the keyboard trusts anything."""
    from undine.store import deps, trust

    d = deps._own_dir(prj.path, args.name)          # a path, or a linked .undine/deps: DepError
    if not d.is_dir():
        print(f"error: {args.name}: no dependency is installed under that name ({deps.DEPS.as_posix()}/{args.name}): "
              f"`python -m undine lib status {prj.name}` lists them, `python -m undine lib sync {prj.name}` installs "
              "them")
        return 1
    key, label, _ = trust._identity(d)
    if not trust.KEY.match(key) or key.endswith("@" + trust._UNKNOWN):
        print(f"error: {args.name} has no install record, so Undine did not install it: `python -m undine lib sync "
              f"{prj.name}` removes or reinstalls it")
        return 1
    try:
        if args.untrust:
            changed = trust.set_trusted(prj, key, False)
            print(f"{key}: no longer trusted in {prj.name}" if changed else f"{key} was not trusted in {prj.name}")
            return 0
        found = trust.scan_dir(d)
        n = sum(trust.counts(found).values())
        print(f"{args.name}: {label}, trust key {key}")
        for group in ("cells", "rtl", "tcl"):
            for where, items in found[group].items():
                for f in items:
                    at = f" line {f.line}" if f.line else ""
                    print(f"  {f.where or where}{at} ({deps._FLAGS.get(f.kind, f.kind)}): {f.text}")
        print(f"{n} finding{'s' if n != 1 else ''}: once trusted, a simulation, LVS and Verilator run what these say"
              if n else "nothing a simulation would refuse was found in it")
        if key in trust.trusted_keys(prj.path):
            print(f"{key} is trusted in {prj.name} already")
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                print(f"error: trusting {key} is a person's decision: run this in a terminal, or pass --yes; nothing "
                      "was changed")
                return 1
            try:
                answer = input(f"trust {key} in {prj.name}? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("not trusted")
                return 1
        trust.set_trusted(prj, key, True)
        print(f"{key}: trusted in {prj.name} (content at another commit, or lib remove, takes it back)")
        return 0
    except ValueError as e:                         # a changeset's preview, a malformed `trusted`
        print(f"error: {e}")
        return 1


def _lock(prj, args) -> list[str]:
    """`lib lock [--format 2]`: resolves what the lock does not pin and records the environment; --format 2 then
    upgrades a format 1 lock (or none) to format 2, the PDK pinned as installed here (pdkops.upgrade). When what it
    resolves needs format 2 (NeedsFormat2: a package's own dependencies, a package under another name), --format 2
    converts in memory and resolves in one write (pdkops.upgrader), so a refusal leaves the format 1 lock as it was.
    Returns the issues it printed (the packages the PDK now pinned does not fit)."""
    from undine.pdks import pdkops
    from undine.store import deps

    try:
        r, upgraded = deps.relock(prj), False
    except deps.NeedsFormat2:
        if args.format != 2:
            raise
        r, upgraded = deps.relock(prj, upgrade=pdkops.upgrader(prj)), True
    declared = prj.library_specs()
    for e in r["resolved"]:
        if e["name"] in declared or not e.get("package"):
            pkg = f", {e['package']} {e.get('version') or '-'}" if e.get("package") else ""
            print(f"{e['name']}: {_entry_text(e)}{pkg}")
        else:                                       # reached only through manifests
            print(_package_line(e))
    print(f"{deps.LOCK}: PDK and tool versions recorded")
    _say_untrusted(prj, [e["name"] for e in r["resolved"]])
    if args.format != 2:
        return []
    if upgraded:
        pin = r["lock"].get("pdk")
        print(f"{deps.LOCK}: format 2, " + (f"PDK {pin['id']} pinned" if isinstance(pin, dict) else "no PDK"))
        return _say_incompatible(pin, _incompatible(prj, r["lock"], pin))
    if deps.lock_format(deps.read_lock(prj.path) or {}) == 2:
        # upgrade would pin the PDK again as it is now: a pin moves only on `pdk verify --pin`, never in passing
        print(f"{deps.LOCK} is format 2 already (`python -m undine pdk verify {prj.name} --pin` pins the PDK again)")
        return []
    up = pdkops.upgrade(prj)
    print(f"{deps.LOCK}: format 2, " + (f"PDK {up['pdk']['id']} pinned" if up["pdk"] else "no PDK"))
    return _say_incompatible(up["pdk"], up.get("incompatible", []))


def _incompatible(prj, lock: dict, pin) -> list[dict]:
    """The locked packages that do not accept the PDK `pin` (as pdkops.upgrade reports them)."""
    from undine.pdks import compat
    from undine.store import deps

    try:
        return compat.locked_incompatible(prj.path, lock, compat.project_target(pin, prj.pdk))
    except (deps.DepError, OSError):                # a linked .undine/deps: status reports it
        return []


def _say_incompatible(pin, packages: list[dict]) -> list[str]:
    """A '!' line per locked package the PDK now pinned does not fit: pinned all the same (the pin records what is
    installed); status keeps it as an issue, in the same words. Returns those issues."""
    pdk_id = pin.get("id") if isinstance(pin, dict) else "(none)"
    said = [f"{u['chain']}: does not fit the PDK {pdk_id}: {u['reason']}: update or remove it (lib update, lib "
            "remove), or choose a PDK it accepts" for u in packages]
    for i in said:
        print(f"! {i}")
    return said


# ------------------------------------------------------------------ pdk


def _print_pdk_check(r: dict) -> int:
    """A PDK verify result (pdkops.verify, pdkops.fetch's): its state, what differs, its warnings. 1 when the PDK is
    not as pinned: modified, missing, another version, a locked library missing."""
    if r.get("previous") and r["previous"] != r["state"]:
        print(f"PDK was {r['previous']} against the old pin")
    for c in r["changed"]:
        print(f"  {c['part']}: " + ("; ".join(f"{k} {', '.join(c[k])}" for k in ("modified", "added", "removed")
                                              if c[k]) or "differs (its files are not known here)"))
    for lib in r["missing_libraries"]:
        print(f"! library {lib} is locked but not installed")
    for lib in r["extra_libraries"]:
        print(f"  library {lib} is installed but not locked")
    for w in r["warnings"]:
        print(f"~ {w}")
    print(f"PDK {r['state']}")
    bad = r["state"] in ("modified", "missing", "other version", "error") or r["missing_libraries"]
    return 1 if bad else 0


def pdk_command(args, store, pdks: dict) -> int:
    from undine.pdks import pdkops

    if args.pdk_cmd == "list":
        for p in list(pdks.values()):
            libs = ",".join(p.libraries()) or "-"
            print(f"{p.id:32} lyp={'yes' if p.lyp else 'no ':3} xschem={'yes' if p.xschem_dir else 'no '}  "
                  f"{p.commit or '-':40}  {p.source or '-':6}  {p.root}  libraries={libs}")
        return 0
    prj = store.get(args.project)
    if args.pdk_cmd == "verify":
        r = pdkops.verify(prj, pin=args.pin, allow_network=args.network)
        if r["created"]:
            _say_created(prj.path)
        elif r["wrote"]:
            print(f"undine.lock: the PDK pinned as installed here (format {r['format']})")
        if r.get("incompatible"):                   # pinned all the same: a warning, the exit code is the PDK's
            from undine.store import deps
            _say_incompatible(deps.lock_pdk(deps.read_lock(prj.path) or {}), r["incompatible"])
        return _print_pdk_check(r)
    return _fetch(prj, args)


def _fetch(prj, args) -> int:
    """`pdk fetch`: shows what ciel would install where, asks (unless --yes), runs it, then compares with the lock."""
    from undine.pdks import pdkops

    plan = pdkops.fetch_plan(prj, args.pdk_root or default_pdk_roots(), allow_network=args.network)
    print(f"fetch the PDK of {prj.name} with ciel:")
    for k in ("family", "variant", "commit", "libraries", "source", "root"):
        v = plan[k]
        if k == "libraries":
            v = ", ".join(v) if v else "ciel default"
        print(f"  {k:<10} {v}")
    if not args.yes:
        try:
            answer = input("fetch it? [y/N] ")
        except EOFError:                            # no terminal, no answer: no
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("not fetched")
            return 1
    r = pdkops.fetch(prj, plan, on_output=lambda line: print(f"  {line}", flush=True))
    print(f"installed {r['path']}")
    code = _print_pdk_check(r["verify"])
    print("a running Undine server finds it after a rescan: Rescan PDKs in the PDK badge's menu, or POST /api/pdks/rescan")
    return code


# ------------------------------------------------------------------ store


def _locks_here(root: Path) -> list[Path]:
    """The directories whose undine.lock and .undine/deps keep store trees alive: the projects under the root, the
    projects that synced elsewhere (the store's list), and every lock in a changeset's worktrees (a project in a git
    repository sits at work/<prefix>, so they are walked). One directory once, however it was reached."""
    from undine.store import cas

    dirs = [p.parent for p in sorted(root.glob("*/project.json"))] + cas.registered_projects()
    for side in sorted(root.glob(".changesets/*/*/work")) + sorted(root.glob(".changesets/*/*/base")):
        for d, sub, files in os.walk(side):
            sub[:] = sorted(x for x in sub if x not in (".git", ".undine"))
            if "undine.lock" in files:
                dirs.append(Path(d))
    seen, out = set(), []
    for d in dirs:
        real = os.path.realpath(d)
        if real not in seen:
            seen.add(real)
            out.append(d)
    return out


def store_command(args, root: Path) -> int:
    from undine.store import cas, deps

    if args.store_cmd == "verify":
        r = cas.verify(dry_run=args.dry_run)
        done = "would be removed" if args.dry_run else "removed"
        for h in r["bad"]:
            print(f"! object {h}: its content does not match its name ({done})")
        for h in r["wrong_mode"]:
            print(f"~ object {h}: was writable ({'would be made' if args.dry_run else 'made'} read-only)")
        for t in r["broken_trees"]:
            print(f"! record {t}: does not hash to its name (then {done}), or names an object the store lacks")
        print(f"{r['checked']} objects checked")
        if r["bad"] or r["broken_trees"]:
            print("`python -m undine lib sync PROJECT` in a project that uses them takes them again from its "
                  "installed files or fetches them")
        return 1 if r["bad"] or r["wrong_mode"] or r["broken_trees"] else 0
    # gc: the live sets are read and the trees removed under one exclusive lock, so no operation (which holds the lock
    # shared from its first tree to its lock write) records a tree in between that gc would not see
    with cas.store_lock(shared=False):
        trees, shas, unreadable = set(), set(), []
        for d in _locks_here(root):
            try:
                lt, ls = deps.locked_trees(deps.read_lock(d))
            except (deps.DepError, OSError) as e:
                unreadable.append(f"{d / deps.LOCK}: {e}")
                continue
            it, ins = deps.installed_trees(d)
            trees |= lt | it
            shas |= ls | ins
        if unreadable:                              # its trees are unknown: removing anything could lose them
            for u in unreadable:
                print(f"! {u}")
            print("nothing was removed: make these locks readable (or remove them), then run store gc again")
            return 1
        r = cas.gc(trees, shas, dry_run=args.dry_run, held=True)
    verb = "would remove" if args.dry_run else "removed"
    print(f"{verb} {r['trees']} tree records and {r['objects']} objects, {_bytes(r['bytes'])}")
    return 0


def _bytes(n: int) -> str:
    for unit in ("bytes", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n} bytes"


def main() -> None:
    ap = argparse.ArgumentParser(prog="undine")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="project directory")
    ap.add_argument("--pdk-root", type=Path, action="append", help="PDK_ROOT style directory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8700)
    s.add_argument("--llm-policy", choices=["both", "cloud", "server", "none"], default=None,
                   help="which LLM connections may be used: cloud (Anthropic), server (OpenAI-compatible, next to this "
                        "server), both (default; one active at a time), none. Also $UNDINE_LLM_POLICY")
    s.add_argument("--mcp", action="store_true", default=bool(os.environ.get("UNDINE_MCP")),
                   help="serve MCP at /mcp for external LLM clients (bearer token in <projects root>/mcp-token); "
                        "never with --llm-policy none")
    s.add_argument("--import-root", type=Path, action="append",
                   help="directories the API may import layout files from (default: home)")
    s.add_argument("--dep-root", type=Path, action="append",
                   help="directories under which the browser and the HTTP API may add or sync a library dependency "
                        "from a local repository (file://, /path, ~/path; default: none). The CLI's `lib` commands "
                        "take any local path")
    s.add_argument("--stop-jobs", action="store_true",
                   help="stop the simulations when this server stops (queued ones cancelled, running ngspice ended). "
                        "Without it they run on, and the next server on the same root takes them up: a restart loses "
                        "no run")
    sub.add_parser("pdks", help="the PDKs found (an alias of pdk list)")
    pk = sub.add_parser("pdk", help="PDKs: list, verify against undine.lock, fetch with ciel").add_subparsers(
        dest="pdk_cmd", required=True)
    pk.add_parser("list", help="the PDKs found, with commit, source and libraries")
    pv = pk.add_parser("verify", help="hash the installed PDK and compare it with undine.lock's pin")
    pv.add_argument("project")
    pv.add_argument("--pin", action="store_true", help="pin the PDK as installed here (writes undine.lock format 2)")
    pf = pk.add_parser("fetch", help="install the project's PDK with ciel into the first ciel root scanned")
    pf.add_argument("project")
    pf.add_argument("--yes", action="store_true", help="do not ask before running ciel")
    for x in (pv, pf):
        x.add_argument("--network", action="store_true",
                       help="ciel may look a commit prefix up at its source (ciel ls-remote)")
    st = sub.add_parser("store", help="the content-addressed store of dependency files").add_subparsers(
        dest="store_cmd", required=True)
    st.add_parser("verify", help="re-hash every object and record; remove the ones that do not match").add_argument(
        "--dry-run", action="store_true", help="report only")
    st.add_parser("gc", help="remove the trees and objects no project here locks or installs").add_argument(
        "--dry-run", action="store_true", help="report only")
    sub.add_parser("doctor")
    sp = sub.add_parser("set-pdk")
    sp.add_argument("project")
    sp.add_argument("pdk")
    n = sub.add_parser("new")
    n.add_argument("project")
    n.add_argument("--pdk")
    i = sub.add_parser("import")
    i.add_argument("project")
    i.add_argument("lib")
    i.add_argument("file", type=Path)
    lb = sub.add_parser("lib", help="library dependencies and undine.lock").add_subparsers(dest="lib_cmd", required=True)
    for name in ("add", "update", "remove", "sync", "lock", "status", "trust"):
        x = lb.add_parser(name)
        x.add_argument("project")
        if name in ("add", "update", "remove", "trust"):
            x.add_argument("name")
        if name == "add":
            x.add_argument("git")
            x.add_argument("--subdir")
            x.add_argument("--package", help="the [package] name the repository's manifest must have")
        if name == "trust":
            x.add_argument("--untrust", action="store_true", help="take the trust back (does not ask)")
            x.add_argument("--yes", action="store_true", help="trust without asking (a person running it)")
        if name in ("add", "update"):
            x.add_argument("--rev")
        if name == "sync":
            x.add_argument("--offline", action="store_true",
                           help="install from the store only, fetch nothing (also UNDINE_OFFLINE=1)")
        if name == "lock":
            x.add_argument("--format", type=int, choices=[2],
                           help="write lock format 2 (the PDK pinned by its tree hashes, the tools with their origin)")
    g = sub.add_parser("git-setup")
    g.add_argument("project")
    ms = sub.add_parser("mcp-stdio", help="local MCP entry for desktop LLM clients: starts or reuses Undine, opens the review")
    ms.add_argument("--url", help="the Undine server (default $UNDINE_URL or http://127.0.0.1:8700); with --ssh, as seen on that host")
    ms.add_argument("--ssh", metavar="HOST", help="Undine runs on HOST (user@host or an ~/.ssh/config alias; default $UNDINE_SSH): "
                                                  "forward a local port to it over SSH and read the token there")
    ms.add_argument("--ui-url", help="where the browser opens Undine, if not the server itself (e.g. the Vite dev server)")
    ms.add_argument("--no-start", action="store_true", help="do not start a server; only use a running one")
    ms.add_argument("--no-open", action="store_true", help="do not open the review in the browser")
    mb = sub.add_parser("mcpb", help="build the Claude Desktop extension (an .mcpb bundle)")
    mb.add_argument("--out", type=Path, help="output file (default dist/undine-<version>.mcpb)")
    from undine.agents.client import add_parsers as client_parsers

    client_parsers(sub)                             # call, tools, session: the tools from the shell
    from undine.agents.skills import add_parser as skills_parser

    skills_parser(sub)
    tc = sub.add_parser("textconv")
    tc.add_argument("file", type=Path)
    md = sub.add_parser("merge-driver", help="the merge driver git-setup registers: git runs it with %%O %%A %%B %%P")
    md.add_argument("--virtual-base", action="store_true",
                    help="the merge of a criss-cross merge's bases (merge.undine-base.driver): the bases' own base "
                         "where they conflict, exit 0, no report")
    for name in ("base", "ours", "theirs", "path"):
        md.add_argument(name)
    rs = sub.add_parser("resolve", help="merge a conflicted file again, taking the named side for those conflicts")
    rs.add_argument("path", help="the conflicted file, from here or from the repository's top (:/PATH: from the top)")
    rs.add_argument("--theirs", nargs="+", action="extend", default=[], metavar="ID", help="take theirs for these")
    rs.add_argument("--ours", nargs="+", action="extend", default=[], metavar="ID", help="take ours for these")
    side = rs.add_mutually_exclusive_group()
    side.add_argument("--all-theirs", dest="all_side", action="store_const", const="theirs")
    side.add_argument("--all-ours", dest="all_side", action="store_const", const="ours")
    mc = sub.add_parser("merge-check", help="a merge's (cherry-pick's, rebase's) open conflicts, warnings and the "
                                            "cells to verify")
    mc.add_argument("dir", nargs="?", type=Path, default=Path("."), help="project directory (default: here)")
    mc.add_argument("--commit", help="a merge commit instead of the merge in progress")
    mc.add_argument("--json", action="store_true")
    sub.add_parser("precommit", help="the pre-commit hook git-setup installs: refuses a commit whose staged "
                                     "operation logs hold unsaved layout edits")
    df = sub.add_parser("diff", help="semantic diff of two files, or of a project between revisions (--rev)")
    df.add_argument("files", nargs="*", type=Path, metavar="FILE", help="OLD NEW")
    df.add_argument("--kind", choices=["schematic", "symbol", "layout", "netlist", "intent", "log"],
                    help="the view (default: from the file name)")
    df.add_argument("--rev", nargs="+", metavar="REV", help="A [B]: from A to B (default: the working tree)")
    df.add_argument("--dir", type=Path, default=Path("."), help="project directory for --rev (default: here)")
    df.add_argument("--json", action="store_true")
    args, extra = ap.parse_known_args()
    if extra and args.cmd == "call" and all("=" in x and not x.startswith("-") for x in extra):
        args.args += extra                          # key=value after an option (call t a=1 --project p b=2)
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")

    if args.cmd == "mcp-stdio":                     # stdout is the MCP channel: no PDK scan, no prints
        from undine.agents.mcp_local import main as mcp_stdio

        mcp_stdio(args)
        return

    if args.cmd in ("call", "tools", "session"):    # a client of a running Undine: no PDK scan
        from undine.agents.client import main as client_main

        client_main(args)

    if args.cmd == "skills":
        from undine.agents.skills import main as skills_main

        skills_main(args)
        return

    if args.cmd == "mcpb":
        from undine.agents.mcpb import build, version

        print(build(args.out or Path("dist") / f"undine-{version()}.mcpb"))
        return

    if args.cmd == "textconv":                      # run by git for every layout file it shows: no PDK scan
        from undine.layouts.layoutstore import textconv

        sys.stdout.write(textconv(args.file))
        return

    if args.cmd in ("merge-driver", "resolve", "merge-check", "precommit", "diff"):   # git's, the shell's: no PDK scan
        raise SystemExit(vcs_command(args))

    pdks = discover(args.pdk_root or default_pdk_roots())
    store = ProjectStore(args.root.expanduser(), pdks)

    if args.cmd == "doctor":
        from undine.doctor import run

        raise SystemExit(run(pdks))
    elif args.cmd == "pdks":
        args.pdk_cmd = "list"
        pdk_command(args, store, pdks)
    elif args.cmd == "pdk":
        raise SystemExit(pdk_command(args, store, pdks))
    elif args.cmd == "store":
        raise SystemExit(store_command(args, store.root))
    elif args.cmd == "set-pdk":
        from undine.pdks.compat import IncompatiblePdk

        try:
            move = store.get(args.project).set_pdk(args.pdk)
        except IncompatiblePdk as e:                # every package that does not accept it, a line each
            print(f"error: {e}")
            raise SystemExit(1)
        old, new = move
        print(f"{args.project}: {old} -> {new}" if old != new else f"{args.project}: already {new}")
        if getattr(move, "created", False):
            _say_created(store.get(args.project).path)
        if old != new:
            print("cells generated from PCells of the old PDK stay until regenerated "
                  "(schematic-driven layout: Regenerate all devices)")
    elif args.cmd == "new":
        prj = store.create(args.project, args.pdk)
        print(f"created {prj.path} (pdk {prj.meta.get('pdk')})")
        _say_created(prj.path)
    elif args.cmd == "import":
        prj = store.get(args.project)
        libs = prj.libraries()
        lib = libs[args.lib] if args.lib in libs else prj.create_library(args.lib)
        cells = import_layout(lib, args.file)
        print(f"imported {len(cells)} cells into {args.project}/{args.lib}")
    elif args.cmd == "lib":
        raise SystemExit(lib_command(store.get(args.project), args))
    elif args.cmd == "git-setup":
        git_setup(store.get(args.project).path)
    elif args.cmd == "serve":
        import signal
        import threading

        import uvicorn

        from undine.server.api import Server, create_app
        from undine.store.changeset import Changesets

        class Serving(uvicorn.Server):
            def handle_exit(self, sig, frame):
                # asked to stop (Ctrl-C, SIGTERM: a restart): the tools that wait for something (wait_for,
                # run_simulation's wait_s, check_specs ...) answer now with what they have, so that no call holds the
                # server up and none is cut (Server.stopping). Its DRC, LVS, RTL ... jobs end now, as interrupted, and
                # none starts any more (Jobs.close: no later server takes one up, and the next one, which may serve
                # already, must not reuse one as still running); in a thread: a signal handler takes no lock
                srv.stopping.set()
                threading.Thread(target=srv.jobs.close, daemon=True).start()
                super().handle_exit(sig, frame)

        static = Path(__file__).resolve().parents[2] / "web" / "dist"
        srv = Server(store.root, pdks, args.import_root or [Path.home()], args.llm_policy, args.mcp,
                     dep_roots=args.dep_root or [], pdk_roots=args.pdk_root or default_pdk_roots())
        srv.stop_jobs = args.stop_jobs  # what a waiting tool tells the agent when the server stops (wait_for)
        # uvicorn ends a shutdown on a signal by raising it again under the handler it found: SIGTERM's default would
        # end the process before the `finally` below hands its simulations over (or stops them: --stop-jobs)
        term = signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        server = None

        def hangup(sig, frame):
            # the terminal or SSH session closed (SIGHUP, which uvicorn leaves alone): a stop as on SIGTERM, not the
            # default's end at once, which would leave the tools of its DRC, LVS, RTL ... runs running (in sessions of
            # their own, the hangup does not reach them) and its simulations not handed over. uvicorn raises it again
            # once it has shut down: then the `finally` below
            if server is not None and not server.should_exit:
                server.handle_exit(sig, frame)
            else:
                sys.exit(0)
        hup = signal.signal(signal.SIGHUP, hangup) if hasattr(signal, "SIGHUP") else None
        try:
            # a merge a server killed mid-way left is finished or taken back first: nothing else writes those files yet
            for which in Changesets(srv).finish_interrupted():
                print(f"undine: the interrupted merge of the changeset {which} was finished or taken back")
            srv.watch_netlists()       # a schematic changed outside Undine (Xschem, git) reaches its netlist right away
            print(f"undine: {len(pdks)} PDKs, projects in {store.root}")
            if srv.jobs.resumed["adopted"] or srv.jobs.resumed["queued"]:
                print(f"undine: simulations taken up from the last server: {len(srv.jobs.resumed['adopted'])} running "
                      f"({', '.join(srv.jobs.resumed['adopted']) or '-'}), {len(srv.jobs.resumed['queued'])} queued")
            elif not srv.jobs.owner:
                print(f"undine: another server still holds the simulations of {store.root}; this one takes them up "
                      "once it has gone")
            if srv.mcp_enabled:
                from undine.agents.mcp import ensure_token, token_path

                ensure_token(store.root)
                print(f"undine: MCP at http://{args.host}:{args.port}/mcp  (bearer token in {token_path(store.root)})")
            server = Serving(uvicorn.Config(create_app(srv, static), host=args.host, port=args.port,
                                            log_level="warning"))
            try:
                server.run()
            except KeyboardInterrupt:
                pass
            if not server.started:
                raise SystemExit(3)    # as uvicorn.run: it could not start (the port is taken ...)
        finally:
            # however it stops (Ctrl-C, SIGTERM, an error), its simulations are handed over to the next server on this
            # root, ngspice still running; --stop-jobs stops them instead. A server killed (SIGKILL) hands over as well
            srv.jobs.shutdown(stop_jobs=args.stop_jobs)
            signal.signal(signal.SIGTERM, term)
            if hup is not None:
                signal.signal(signal.SIGHUP, hup)
            if not args.stop_jobs and (srv.jobs.active or srv.jobs.waiting):
                print(f"undine: simulations left to the next server on this root: {len(srv.jobs.active)} running, "
                      f"{len(srv.jobs.waiting)} queued (--stop-jobs stops them with the server)")


def cli() -> None:
    """main() with the expected errors (no such project, invalid name, unreadable file ...) as one line, exit 1."""
    import sys

    from undine.store.cas import StoreError
    from undine.store.deps import DepError

    try:
        main()
    # DepError: e.g. set-pdk on a newer lock; PinError is a ValueError, CielError and GitSourceError RuntimeErrors
    except (KeyError, ValueError, RuntimeError, OSError, DepError, StoreError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        print(f"undine: {msg}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
