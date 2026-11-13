"""The git plumbing of changesets on branches (vcs/branches.py, docs/design/changeset-branches.md): a project in no
repository is set up and one in a repository nobody set up is refused, the base commit of a working tree as it is,
libraries a worktree cannot hold refused, worktrees and their commits (git's stat cache: an unchanged file is not read
again), removal (also of a worktree deleted by hand), the exclusion of the store's folder of worktrees, the plan of an
apply or a revert and its atomic write, the merge commit on the user's branch, the first commit of an unborn one,
the semantic diff of a base; no hook runs and no git identity is needed. With real git in temporary directories."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import klayout.db as kdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.__main__ import git_setup  # noqa: E402
from undine.layouts import layoutstore  # noqa: E402
from undine.schematics import xschem  # noqa: E402
from undine.vcs import branches  # noqa: E402
from test_vcs_log import jsonl, parse, rec  # noqa: E402
from test_vcs_schematic import INV, INV_SYM, edit  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

MET1 = (68, 20)
HEADER = "v {xschem version=3.4.5 file_version=1.2\n}\nG {}\nK {}\nV {}\nS {}\nE {}\n"
BUF = HEADER + "C {inv.sym} 0 0 0 0 {name=x1}\nC {inv.sym} 200 0 0 0 {name=x2}\n"
TOP = HEADER + "C {buf.sym} 0 0 0 0 {name=x1}\n"
SCH = "libs/l/inv.sch"
M1_W3 = ("name=M1\nL=0.15\nW=2", "name=M1\nL=0.15\nW=3")       # theirs' edit of M1
M2_L = ("name=M2\nL=0.15", "name=M2\nL=0.18")                  # ours' edit of M2
M2_AT = "{sky130_fd_pr/nfet_01v8.sym} -20 60 0 0"
OWN = ("runs/j1/status.json", "tapeout/top.gds", "llm/audit.jsonl", ".undine/deps/lib/x.sch",
       "libs/l/layout.oas.pre-split", "changesets/cs-1.json", "changesets/cs-1/evidence/manifest.json")


@pytest.fixture(autouse=True)
def git_env(monkeypatch, tmp_path):
    """git without the user's or the system's configuration, and never a repository above the test's directory."""
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CEILING_DIRECTORIES": str(tmp_path)}
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def git(d: Path, *a: str, ok: bool = True) -> str:
    r = subprocess.run(["git", *a], cwd=d, capture_output=True, text=True)
    assert not ok or r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def write(d: Path, rel: str, data: str | bytes) -> None:
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)


def layout(lib: Path, edit_fn=None) -> None:
    """inv (metal, a via1 instance) and top (inv as X1), one file per cell; or an edit of the saved cells, written
    the way the layout store saves them (only the cells that changed)."""
    ly = kdb.Layout()
    if edit_fn is None:
        ly.dbu = 0.001
        inv, via, top = (ly.create_cell(n) for n in ("inv", "via1", "top"))
        via.shapes(ly.layer(67, 44)).insert(kdb.Box(0, 0, 170, 170))
        inv.shapes(ly.layer(*MET1)).insert(kdb.Box(0, 0, 500, 2000))
        inv.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans(100, 100)))
        top.insert(kdb.CellInstArray(inv.cell_index(), kdb.Trans(0, 0))).set_property("undine_inst", "X1")
        layoutstore.write(lib, ly, {})
        return
    layoutstore.read(lib, ly)
    saved = layoutstore.snapshot(ly)
    edit_fn(ly)
    layoutstore.write(lib, ly, saved)


def add_box(cell: str, *box: int):
    return lambda ly: ly.cell(cell).shapes(ly.layer(*MET1)).insert(kdb.Box(*box))


def boxes(path: Path, cell: str) -> list[str]:
    ly = kdb.Layout()
    ly.read(str(path))
    return sorted(s.to_s() for s in ly.cell(cell).shapes(ly.layer(*MET1)).each())


def comp(data: str, name: str) -> xschem.XComp:
    return next(c for c in xschem.parse(data).comps if c.props.get("name") == name)


def project(d: Path, setup: bool = True, commit: bool = True) -> Path:
    """Project p, library l: inv (schematic, symbol, layout with the child cell via1), buf, top above buf (layout
    above inv), and the operation log; set up for Undine (git-setup), committed."""
    write(d, "project.json", json.dumps({"name": "p", "format": 3, "pdk": None, "libraries": {"l": "libs/l"}}))
    write(d, SCH, INV)
    write(d, "libs/l/inv.sym", INV_SYM)
    write(d, "libs/l/buf.sch", BUF)
    write(d, "libs/l/buf.sym", INV_SYM)
    write(d, "libs/l/top.sch", TOP)
    write(d, "history/l.jsonl", jsonl(rec(1, reason="size the inverter")))
    layout(d / "libs/l")
    if setup:
        git_setup(d, say=None)
    if commit:
        git(d, "add", "-A")
        git(d, "commit", "-qm", "base")
    return d


def libraries(d: Path, **more: str) -> None:
    """project.json's libraries: l, and `more` (name: path)."""
    meta = json.loads((d / "project.json").read_text())
    meta["libraries"] = {"l": "libs/l", **more}
    write(d, "project.json", json.dumps(meta))


def head(d: Path) -> str:
    return git(d, "rev-parse", "HEAD")


def parents(d: Path, sha: str) -> list[str]:
    return git(d, "rev-list", "--parents", "-n", "1", sha).split()[1:]


def files(d: Path, rev: str) -> set[str]:
    """A commit's files from the project directory."""
    return set(git(d, "ls-tree", "-r", "--name-only", rev).splitlines())


def show(d: Path, rev: str, rel: str) -> str:
    return git(d, "show", f"{rev}:./{rel}")


def tree_state(d: Path) -> dict[str, bytes]:
    """Every file of the directory but git's: its bytes."""
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(d).parts}


def views(diff: dict) -> dict[tuple[str, str, str], str]:
    return {(c["lib"], c["cell"], v["view"]): v["status"] for c in diff["cells"] for v in c["views"]}


def opened(d: Path, tmp_path: Path, cid: str = "cs-1") -> tuple[str, Path]:
    """A changeset opened as the backend does: the base commit and its branch's worktree (the project in it)."""
    base = branches.snapshot(d, f"Open {cid}", "agent:claude")
    return base, branches.add_worktree(d, tmp_path / f".changesets/p/{cid}/work", base, f"undine/p/{cid}")


def committed(work: Path, msg: str = "edit") -> str:
    sha = branches.commit_worktree(work, "", msg, "agent:claude")
    assert sha
    return sha


# ---------------------------------------------------------------- the repository


def test_a_project_in_no_repository_is_set_up_with_nothing_committed(tmp_path):
    d = project(tmp_path / "p", setup=False, commit=False)
    with pytest.raises(branches.NoRepo):
        branches.repo(d)
    assert branches.ensure_repo(d) == {"git_setup": True}
    r = branches.repo(d)
    assert r.set_up and r.head is None and r.branch and r.prefix == "" and r.top.resolve() == d.resolve()
    assert r.common_dir == (d / ".git").resolve() and r.git_dir.resolve() == (d / ".git").resolve()
    assert "merge=undine" in (d / ".gitattributes").read_text() and "/runs/" in (d / ".gitignore").read_text()
    assert git(d, "ls-files") == ""                                         # nothing staged, nothing committed
    assert branches.ensure_repo(d) == {"git_setup": False}


def test_a_repository_not_set_up_for_undine_is_refused_and_left_alone(tmp_path, monkeypatch):
    home = tmp_path / "home"
    write(home, ".bashrc", "export X=1\n")
    git(home, "init", "-q")
    config = (home / ".git/config").read_text()
    d = project(home / "undine-projects/p", setup=False, commit=False)
    # the command names the project (`undine git-setup` alone stops at its missing argument), and the store's root
    # when it is not the command's default one
    monkeypatch.setattr("undine.__main__.DEFAULT_ROOT", tmp_path / "elsewhere")
    with pytest.raises(branches.NotSetUp, match=r"set it up with `undine git-setup p` \(in this store: `undine --root "
                       rf"{re.escape(str(home / 'undine-projects'))} git-setup p`; Set up, in the History panel\), or "
                       r"move the project out of it$") as refused:
        branches.ensure_repo(d)
    # what the API answers with: 400 {"detail", "code": "not_set_up", "top", "project"} (the History panel's button
    # there, with the merge driver unset in a repository, is Set up: Set up git is the one of a project in none)
    assert refused.value.code == "not_set_up" and refused.value.top.resolve() == home.resolve()
    assert refused.value.project == "p"
    assert refused.value.command == f"undine --root {home / 'undine-projects'} git-setup p"     # as the screen gives it
    monkeypatch.setattr("undine.__main__.DEFAULT_ROOT", home / "undine-projects")
    with pytest.raises(branches.NotSetUp, match=r"set it up with `undine git-setup p` \(Set up, in the History "
                       r"panel\), or move the project out of it$") as refused:
        branches.ensure_repo(d)
    assert refused.value.command == "undine git-setup p"
    assert not (d / ".gitattributes").exists() and (home / ".git/config").read_text() == config
    r = branches.repo(d)
    assert not r.set_up and r.prefix == "undine-projects/p" and r.top.resolve() == home.resolve()


def test_one_lock_per_repository_reentrant(tmp_path):
    d = project(tmp_path / "p")
    base, work = opened(d, tmp_path)
    assert branches._lock_of(d) is branches._lock_of(work)                 # the worktree shares it
    other = project(tmp_path / "q")
    assert branches._lock_of(other) is not branches._lock_of(d)
    with branches.lock(d) as r:
        assert r.head == head(d) and r.branch == git(d, "symbolic-ref", "--short", "HEAD")
        assert branches.snapshot(d, "again", "user")                       # reentrant
        got = []
        t = threading.Thread(target=lambda: got.append(branches.snapshot(work, "other thread", "user")))
        t.start()
        t.join(0.5)
        assert t.is_alive() and not got                                    # waits for the repository's lock
    t.join(10)
    assert got


# ---------------------------------------------------------------- the base commit


def test_snapshot_is_the_working_tree_as_it_is_and_touches_nothing(tmp_path):
    d = project(tmp_path / "p")
    write(d, "changesets/old.json", "{}")                                   # a record the user committed
    git(d, "add", "-A")
    git(d, "commit", "-qm", "records")
    first = head(d)
    write(d, SCH, edit(INV, *M1_W3))                                        # staged, then edited again
    git(d, "add", SCH)
    write(d, SCH, edit(INV, M1_W3[0], "name=M1\nL=0.15\nW=4"))
    write(d, "libs/l/new.sch", HEADER)                                      # untracked
    (d / "libs/l/buf.sch").unlink()                                         # deleted, not staged
    (d / ".gitignore").unlink()                                             # Undine's output not even ignored
    for rel in OWN:
        write(d, rel, "own")
    state = lambda: (git(d, "status", "--porcelain=v1", "--untracked-files=all"), git(d, "ls-files", "-s"),
                     head(d), git(d, "symbolic-ref", "HEAD"), git(d, "for-each-ref"))
    before = state()
    sha = branches.snapshot(d, "Open cs-1: size M1\n\nwhy", "agent:claude")
    assert state() == before and tree_state(d)["libs/l/new.sch"] == HEADER.encode()
    assert parents(d, sha) == [first]
    fs = files(d, sha)
    assert "W=4" in show(d, sha, SCH) and "libs/l/new.sch" in fs and "libs/l/buf.sch" not in fs
    assert ".gitignore" not in fs and {"project.json", "libs/l/inv.oas", "history/l.jsonl"} <= fs
    assert not any(f.startswith(("runs/", "tapeout/", "llm/", ".undine/", "changesets/")) or f.endswith(".pre-split")
                   for f in fs)
    assert git(d, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%B", sha) == \
        "agent:claude <undine@localhost>|Undine <undine@localhost>|Open cs-1: size M1\n\nwhy"
    assert not list((d / ".git").glob("undine-index-*"))                   # the temporary index is gone


def test_snapshot_of_a_file_made_a_directory_and_of_symbolic_links(tmp_path):
    d = project(tmp_path / "p")
    write(d, "doc/spec", "a file\n")
    git(d, "add", "doc/spec")
    git(d, "commit", "-qm", "spec")
    (d / "doc/spec").unlink()
    write(d, "doc/spec/a.txt", "a directory now\n")
    write(tmp_path / "shared", "cells.txt", "elsewhere\n")
    os.symlink(tmp_path / "shared", d / "doc/shared")                      # a link to a directory: stored as such
    sha = branches.snapshot(d, "Open cs-1", "user")
    entries = {path: meta for meta, path in (line.split("\t") for line in git(d, "ls-tree", "-r", sha, "doc")
                                                .splitlines())}
    assert set(entries) == {"doc/spec/a.txt", "doc/shared"} and entries["doc/shared"].startswith("120000 blob")
    # a directory made a symbolic link, and one made a file: what the index has in them leaves it first (git
    # refuses to add the link or the file over them)
    write(d, "notes/a/x.txt", "x\n")
    write(d, "drafts/b/y.txt", "y\n")
    write(d, "kept/a/x.txt", "kept\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "notes")
    shutil.rmtree(d / "notes")
    os.symlink("kept", d / "notes")
    shutil.rmtree(d / "drafts")
    write(d, "drafts", "a file now\n")
    sha = branches.snapshot(d, "Open cs-1", "user")
    entries = {path: meta for meta, path in (line.split("\t") for line in git(d, "ls-tree", "-r", sha, "notes",
                                                                              "drafts").splitlines())}
    assert set(entries) == {"notes", "drafts"} and entries["notes"].startswith("120000 blob")
    assert show(d, sha, "drafts") == "a file now" and git(d, "ls-files", "notes", "drafts") == \
        "drafts/b/y.txt\nnotes/a/x.txt"                                     # the user's index as it was


def test_snapshot_on_an_unborn_branch(tmp_path):
    d = project(tmp_path / "p", setup=False, commit=False)
    branches.ensure_repo(d)
    sha = branches.snapshot(d, "Open cs-1", "user")
    assert parents(d, sha) == [] and {SCH, ".gitattributes", ".gitignore", "project.json"} <= files(d, sha)
    assert branches.repo(d).head is None and git(d, "ls-files") == ""


def test_snapshot_reads_what_the_user_told_git_not_to_look_at(tmp_path):
    """The temporary index starts from the user's (stat cache): entries marked assume-unchanged or skip-worktree
    there are read all the same, and the user's index keeps its marks."""
    d = project(tmp_path / "p")
    write(d, SCH, edit(INV, *M1_W3))
    git(d, "update-index", "--assume-unchanged", SCH)
    write(d, "libs/l/buf.sch", BUF + "C {inv.sym} 400 0 0 0 {name=x3}\n")
    git(d, "update-index", "--skip-worktree", "libs/l/buf.sch")
    sha = branches.snapshot(d, "Open cs-1", "user")
    assert "W=3" in show(d, sha, SCH) and "name=x3" in show(d, sha, "libs/l/buf.sch")
    assert git(d, "ls-files", "-v", SCH, "libs/l/buf.sch").split() == ["S", "libs/l/buf.sch", "h", SCH]


def test_snapshot_of_a_project_whose_index_has_a_conflict(tmp_path):
    """An index git cannot merge HEAD into (a merge stopped on a conflict): every file is read, as without one."""
    d = project(tmp_path / "p")
    git(d, "checkout", "-q", "-b", "side")
    write(d, "notes.txt", "side\n")
    git(d, "add", "notes.txt")
    git(d, "commit", "-qm", "side")
    git(d, "checkout", "-q", "-")
    write(d, "notes.txt", "ours\n")
    git(d, "add", "notes.txt")
    git(d, "commit", "-qm", "ours")
    git(d, "merge", "-q", "side", ok=False)
    assert "notes.txt" in git(d, "ls-files", "-u")                       # unmerged
    staged = git(d, "ls-files", "-s")
    sha = branches.snapshot(d, "Open cs-1", "user")
    assert "<<<<<<<" in show(d, sha, "notes.txt") and parents(d, sha) == [head(d)]
    assert git(d, "ls-files", "-s") == staged                              # the user's index as it was


# ---------------------------------------------------------------- libraries a worktree cannot hold


def test_a_library_linked_out_of_the_project_is_refused(tmp_path):
    """git stores a symbolic link as it is: a worktree's would lead to the real files (or nowhere), and the
    proposal would write through it. Opening is refused, naming the library."""
    d = project(tmp_path / "p")
    outside = tmp_path / "shared"
    write(outside, "amp.sch", INV)
    write(tmp_path, "models/m.spice", "* models\n")
    outer = lambda: {k: v for k, v in tree_state(tmp_path).items() if not k.startswith("p/")}
    before = outer()
    cases = [("libs/shared", str(outside), "libs/shared", "outside the project", "link_out"),        # absolute
             ("libs/shared", "../../shared", "libs/shared", "outside the project", "link_out"),       # relative
             ("libs/shared", "../nowhere", "libs/shared", "which does not exist", "link_nowhere"),
             ("ext/shared", str(tmp_path), "ext", "outside the project", "link_out")]       # a folder above it
    for lib, target, link, where, kind in cases:
        libraries(d, shared=lib)
        os.symlink(target, d / link)
        does = "write through it into the files there" if kind == "link_out" else "find nothing there"
        with pytest.raises(branches.Unsupported, match=rf"^library shared: {link} is a symbolic link to "
                           rf"{re.escape(target)}, {where}: a changeset's working copy \(a git worktree\) would hold "
                           rf"the link and {does}.*Put the files themselves in the project") as refused:
            branches.snapshot(d, "Open cs-1", "agent:claude")
        # what the API answers with: 400 {"detail", "code": "unsupported_library", "library", "path", "kind",
        # "target"}
        e = refused.value
        assert (e.code, e.library, e.path, e.kind, e.target) == ("unsupported_library", "shared", link, kind, target)
        os.unlink(d / link)
    libraries(d, shared="libs/shared")                                     # a link in it
    write(d, "libs/shared/amp.sch", INV)
    os.symlink(tmp_path / "models/m.spice", d / "libs/shared/m.spice")
    with pytest.raises(branches.Unsupported, match="^library shared: libs/shared/m.spice is a symbolic link to "):
        branches.snapshot(d, "Open cs-1", "agent:claude")
    os.unlink(d / "libs/shared/m.spice")
    assert tree_state(tmp_path / "shared") == {"amp.sch": INV.encode()}
    assert outer() == before
    sha = branches.snapshot(d, "Open cs-1", "agent:claude")               # the files themselves: opened
    assert "libs/shared/amp.sch" in files(d, sha)


def test_a_library_linked_inside_the_project_leads_into_the_worktree(tmp_path):
    d = project(tmp_path / "p")
    libraries(d, alias="libs/alias")
    os.symlink("l", d / "libs/alias")
    base, work = opened(d, tmp_path)
    assert (work / "libs/alias").is_symlink() and (work / "libs/alias/inv.sch").read_text() == INV
    write(work, "libs/alias/inv.sch", edit(INV, *M1_W3))                   # the worktree's l, not the project's
    assert (d / SCH).read_text() == INV and "W=3" in show(d, committed(work), SCH)


def test_a_library_link_is_judged_as_the_worktree_will_resolve_it(tmp_path):
    """git checks a link out as its text says, from the worktree's folder: one that leads into the project as the
    project has it may not in the worktree. An absolute path leads to the project's own files (the proposal would
    write into them, and review would show nothing); a path that climbs out of the project and back in leads
    elsewhere from the worktree; what the tree does not keep (ignored, Undine's own output) is not in the worktree,
    the link leads nowhere there. Refused, naming the link, before the worktree is made."""
    d = project(tmp_path / "p")
    for rel in ("vendor/shared/amp.sch", "store/shared/amp.sch", "data/shared/amp.sch", "llm/shared/amp.sch",
                "common/amp.sch"):
        write(d, rel, INV)
    write(d, "data/m.spice", "* models\n")
    write(d, "models/m.spice", "* models\n")
    write(d, ".gitignore", (d / ".gitignore").read_text() + "/data/\n")
    git(d, "add", "-A")
    git(d, "commit", "-qm", "vendor")
    state = tree_state(d)
    libraries(d, shared="libs/shared")
    cases = [   # (link, its text, the path refused, its text, why, kind)
        ("libs/shared", str(d / "vendor/shared"), None, None, "an absolute path", "link_out"),
        ("libs/shared", "../../p/vendor/shared", None, None, "a path that climbs out of the project", "link_out"),
        ("libs/shared", "../data/shared", None, None, "which leads to data/shared, what the project's repository "
         "does not keep", "link_nowhere"),
        ("libs/shared", "../llm/shared", None, None, "which leads to llm/shared, what the project's repository does "
         "not keep", "link_nowhere"),                                       # Undine's own output: never in a tree
        ("libs/shared", "../vendor/shared", "vendor", str(d / "store"), "an absolute path", "link_out"),  # on the way
        ("libs/shared/m.spice", "../../data/m.spice", None, None, "which leads to data/m.spice", "link_nowhere"),
        ("libs/shared/m.spice", "../../../p/models/m.spice", None, None, "a path that climbs out", "link_out"),
        ("libs/l/amp.sch", str(d / "common/amp.sch"), None, None, "an absolute path", "link_out"),   # in library l
    ]
    for link, text, other, other_text, why, kind in cases:
        if link == "libs/shared/m.spice":
            write(d, "libs/shared/amp.sch", INV)
        if other:
            shutil.move(d / other, d / "moved")
            os.symlink(other_text, d / other)
        os.symlink(text, d / link)
        path, target = other or link, other_text or text
        name = "l" if link.startswith("libs/l/") else "shared"
        with pytest.raises(branches.Unsupported, match=rf"^library {name}: {path} is a symbolic link to "
                           rf"{re.escape(target)}, {why}") as refused:
            branches.snapshot(d, "Open cs-1", "agent:claude")
        e = refused.value
        assert (e.library, e.path, e.kind, e.target) == (name, path, kind, target), link
        os.unlink(d / link)
        if other:
            os.unlink(d / other)
            shutil.move(d / "moved", d / other)
        shutil.rmtree(d / "libs/shared", ignore_errors=True)
    # a link the tree does not keep itself: the worktree has nothing there
    write(d, ".gitignore", (d / ".gitignore").read_text() + "/libs/shared\n")
    os.symlink("../vendor/shared", d / "libs/shared")
    with pytest.raises(branches.Unsupported, match=r"^library shared: libs/shared is a symbolic link to "
                       r"\.\./vendor/shared, a link the project's repository does not keep") as refused:
        branches.snapshot(d, "Open cs-1", "agent:claude")
    assert refused.value.kind == "link_nowhere"
    assert tree_state(d) == {**state, ".gitignore": (d / ".gitignore").read_bytes(),
                             "project.json": (d / "project.json").read_bytes()}     # the project as it was
    write(d, ".gitignore", (d / ".gitignore").read_text().replace("/libs/shared\n", ""))
    base, work = opened(d, tmp_path)                                        # relative, inside, kept: opened
    assert os.readlink(work / "libs/shared") == "../vendor/shared"
    write(work, "libs/shared/amp.sch", edit(INV, *M1_W3))                  # into the worktree's copy
    assert (d / "vendor/shared/amp.sch").read_text() == INV and "W=3" in show(d, committed(work),
                                                                           "vendor/shared/amp.sch")


def test_check_libraries_needs_no_repository(tmp_path):
    """What the folder tells is refused before a repository is made (ensure_repo would git init the project): the
    API's refusal leaves a project in no repository as it was."""
    d = project(tmp_path / "p", setup=False, commit=False)
    libraries(d, shared="libs/shared")
    write(tmp_path, "shared/amp.sch", INV)
    os.symlink("../../shared", d / "libs/shared")
    with pytest.raises(branches.Unsupported, match="^library shared: libs/shared is a symbolic link to "
                       r"\.\./\.\./shared, outside the project") as refused:
        branches.check_libraries(d)
    assert refused.value.code == "unsupported_library" and refused.value.kind == "link_out"
    assert not (d / ".git").exists() and not (d / ".gitattributes").exists()
    os.unlink(d / "libs/shared")
    os.symlink("l", d / "libs/shared")
    assert branches.check_libraries(d) == [branches.LibraryFolder("l", "libs/l", ()),
                                           branches.LibraryFolder("shared", "libs/l", (("libs/shared", "l"),))]
    assert not (d / ".git").exists()


def test_a_library_that_is_a_repository_of_its_own_is_refused(tmp_path):
    """git stores a repository in the project as a gitlink, which a new worktree holds empty: opening is refused,
    naming the library, instead of an empty library in the proposal and its baseline."""
    d = project(tmp_path / "p")
    libraries(d, shared="libs/shared")
    lib = d / "libs/shared"
    write(lib, "inv.sym", INV_SYM)
    git(lib, "init", "-q")
    git(lib, "add", "-A")
    git(lib, "commit", "-qm", "its own")
    with pytest.raises(branches.Unsupported, match=r"^library shared: libs/shared is a git repository of its own "
                       r"\(a clone or a submodule\), which a changeset's working copy \(a git worktree\) would hold "
                       r"empty. Make it a dependency .*, or let the project's repository keep its files \(move its "
                       r"\.git out of the project\)") as refused:
        branches.snapshot(d, "Open cs-1", "agent:claude")
    e = refused.value
    assert (e.code, e.library, e.path, e.kind, e.target) == \
        ("unsupported_library", "shared", "libs/shared", "own_repository", None)
    shutil.rmtree(lib / ".git")
    write(lib, ".git", "gitdir: ../../.git/modules/shared\n")              # a submodule's
    with pytest.raises(branches.Unsupported, match="^library shared: libs/shared is a git repository of its own"):
        branches.snapshot(d, "Open cs-1", "agent:claude")
    (lib / ".git").unlink()
    sub = lib / "cells"                                                     # one in the library
    write(sub, "buf.sym", INV_SYM)
    git(sub, "init", "-q")
    git(sub, "add", "-A")
    git(sub, "commit", "-qm", "its own")
    with pytest.raises(branches.Unsupported, match="^library shared: libs/shared/cells is a git repository"):
        branches.snapshot(d, "Open cs-1", "agent:claude")
    shutil.rmtree(sub / ".git")
    notes = d / "doc/notes"                                                 # one elsewhere: as git stores it
    write(notes, "a.txt", "notes\n")
    git(notes, "init", "-q")
    git(notes, "add", "-A")
    git(notes, "commit", "-qm", "notes")
    sha = branches.snapshot(d, "Open cs-1", "agent:claude")
    assert {"libs/shared/inv.sym", "libs/shared/cells/buf.sym"} <= files(d, sha)
    assert git(d, "ls-tree", sha, "doc/notes").startswith("160000 commit")
    base, work = opened(d, tmp_path)
    assert (work / "libs/shared/inv.sym").read_text() == INV_SYM


def test_a_project_in_a_subdirectory_of_its_repository(tmp_path):
    repo = tmp_path / "repo"
    write(repo, "README", "designs\n")
    git(repo, "init", "-q")
    d = project(repo / "designs/p", commit=False)                          # git-setup: the repository it is in
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    r = branches.repo(d)
    assert r.prefix == "designs/p" and r.set_up and branches.ensure_repo(d) == {"git_setup": False}
    write(repo, "README", "edited, not committed\n")                       # outside the project: HEAD's
    write(repo, "other/x", "untracked outside\n")
    write(d, SCH, edit(INV, *M2_L))
    base = branches.snapshot(d, "Open cs-1", "user")
    assert git(repo, "show", f"{base}:README") == "designs" and "other/x" not in files(repo, base)
    assert "L=0.18" in show(d, base, SCH)

    wt = tmp_path / "shadow/cs-1/work"
    work = branches.add_worktree(d, wt, base, "undine/p/cs-1")
    assert work == wt / "designs/p" and (work / SCH).read_text() == edit(INV, *M2_L)
    write(work, SCH, edit(edit(INV, *M2_L), *M1_W3))
    write(wt, "README", "edited in the worktree\n")                        # outside the project: never committed
    tip = branches.commit_worktree(wt, "designs/p", "size M1", "agent:claude")
    assert tip and parents(d, tip) == [base] and git(repo, "show", f"{tip}:README") == "designs"
    assert git(repo, "diff", "--name-only", base, tip) == f"designs/p/{SCH}"
    assert git(wt, "status", "--porcelain=v1") == "M README"                # the index is the tip's

    plan = branches.plan_merge(d, base, tip)
    assert plan["paths"] == [SCH] and list(plan["writes"]) == [SCH] and plan["conflicts"] == []
    assert views(branches.diff(work, base)) == {("l", "inv", "schematic"): "modified"}
    assert branches.write_plan(d, plan) == [SCH]
    assert comp((d / SCH).read_text(), "M1").props["W"] == "3"


# ---------------------------------------------------------------- worktrees


def test_worktrees_their_commits_and_the_kept_branch(tmp_path):
    d = project(tmp_path / "p")
    shadow = tmp_path / ".changesets/p/cs-1"
    base, work = opened(d, tmp_path)
    baseline = branches.add_worktree(d, shadow / "base", base)
    assert work == shadow / "work" and baseline == shadow / "base"
    assert branches.resolve(d, "undine/p/cs-1") == base and branches.ref_exists(d, "undine/p/cs-1")
    assert branches.repo(work).branch == "undine/p/cs-1" and branches.repo(work).head == base
    assert branches.repo(baseline).branch is None and branches.repo(baseline).head == base     # detached
    assert (work / SCH).read_text() == INV and branches.resolve(d, "nothing") is None

    assert branches.commit_worktree(work, "", "nothing yet", "agent:claude") is None
    write(work, SCH, edit(INV, *M1_W3))
    write(work, "libs/l/new.sch", HEADER)
    (work / "libs/l/buf.sch").unlink()
    for rel in OWN:
        write(work, rel, "own")
    tip = branches.commit_worktree(work, "", "size M1", "agent:claude")
    assert parents(d, tip) == [base] and branches.resolve(d, "undine/p/cs-1") == tip
    fs = files(d, tip)
    assert "libs/l/new.sch" in fs and "libs/l/buf.sch" not in fs and "W=3" in show(d, tip, SCH)
    assert not any(f.startswith(("runs/", "changesets/", "llm/", "tapeout/", ".undine/")) for f in fs)
    assert git(d, "log", "-1", "--format=%an|%cn|%s", tip) == "agent:claude|Undine|size M1"
    # the worktree's index follows its branch: only what is never committed is left (the evidence is ignored)
    assert git(work, "status", "--porcelain=v1", "--untracked-files=all").splitlines() == \
        ["?? changesets/cs-1.json"]
    assert git(work, "diff", "--cached", "--name-only") == ""
    assert branches.commit_worktree(work, "", "again", "agent:claude") is None
    assert (d / SCH).read_text() == INV and git(d, "status", "--porcelain") == ""      # the project untouched

    branches.remove_worktree(d, shadow / "work")
    branches.remove_worktree(d, shadow / "base")
    assert not (shadow / "work").exists() and not (shadow / "base").exists()
    assert git(d, "worktree", "list", "--porcelain").count("worktree ") == 1
    branches.remove_worktree(d, shadow / "work")                           # gone already: no error
    assert branches.keep_ref(d, "undine/p/cs-1", "refs/undine/changesets/p/cs-1") == tip
    assert not branches.ref_exists(d, "undine/p/cs-1")
    assert branches.resolve(d, "refs/undine/changesets/p/cs-1") == tip
    assert branches.keep_ref(d, "undine/p/cs-1", "refs/undine/changesets/p/cs-1") == tip      # moved before
    with pytest.raises(ValueError, match="refs/undine"):
        branches.keep_ref(d, "undine/p/cs-1", "refs/heads/kept")
    with pytest.raises(ValueError, match="no branch"):
        branches.keep_ref(d, "undine/p/cs-9", "refs/undine/changesets/p/cs-9")


def test_a_commit_moves_the_branch_only_from_the_tip_it_read(tmp_path, monkeypatch):
    d = project(tmp_path / "p")
    base, work = opened(d, tmp_path)
    write(work, SCH, edit(INV, *M1_W3))
    other = branches.snapshot(d, "someone else", "user")
    commit = branches._commit

    def racing(*a, **kw):                                                   # the branch moves meanwhile
        git(d, "update-ref", "refs/heads/undine/p/cs-1", other)
        return commit(*a, **kw)
    monkeypatch.setattr(branches, "_commit", racing)
    with pytest.raises(RuntimeError, match="update-ref"):
        branches.commit_worktree(work, "", "size M1", "agent:claude")
    assert branches.resolve(d, "undine/p/cs-1") == other


def test_worktrees_deleted_by_hand(tmp_path):
    d = project(tmp_path / "p")
    shadow = tmp_path / ".changesets/p"
    base, work = opened(d, tmp_path, "cs-1")
    branches.add_worktree(d, shadow / "cs-1/base", base)
    shutil.rmtree(shadow / "cs-1")
    branches.remove_worktree(d, shadow / "cs-1/work")                      # tolerated, and pruned
    assert git(d, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert branches.keep_ref(d, "undine/p/cs-1", "refs/undine/changesets/p/cs-1") == base

    _, work2 = opened(d, tmp_path, "cs-2")
    with pytest.raises(ValueError, match="checked out"):                    # live: not moved under it
        branches.keep_ref(d, "undine/p/cs-2", "refs/undine/changesets/p/cs-2")
    shutil.rmtree(shadow / "cs-2")
    branches.prune(d)
    assert git(d, "worktree", "list", "--porcelain").count("worktree ") == 1
    git(d, "branch", "-D", "undine/p/cs-2")                                # checked out nowhere any more


def test_a_worktree_inside_the_repository_is_excluded_while_it_exists(tmp_path):
    root = tmp_path / "root"                                                # the store's root kept in git
    write(root, "README", "projects\n")
    git(root, "init", "-q")
    d = project(root / "p", commit=False)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    exclude = lambda: (root / ".git/info/exclude").read_text().splitlines()
    base = branches.snapshot(d, "Open cs-1", "user")
    shadow = root / ".changesets/p/cs-1"
    branches.add_worktree(d, shadow / "work", base, "undine/p/cs-1")
    branches.add_worktree(d, shadow / "base", base)
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    # what is kept beside the worktrees (the tip, the floor, a batch's or a restore's snapshot) is the store's too
    write(shadow, "tip.json", "{}\n")
    write(shadow, "gseq.floor", "7\n")
    write(shadow, ".batch-x1/libs/l/inv.sch", INV)
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    git(root, "add", "-A")                                                  # the user's add: nothing of it
    assert git(root, "diff", "--cached", "--name-only") == ""
    assert "/.changesets/" in exclude() and not any("cs-1" in x for x in exclude())

    opened(d, root, "cs-2")                                                 # another changeset there
    branches.remove_worktree(d, shadow / "work")
    branches.remove_worktree(d, shadow / "base")
    assert "/.changesets/" in exclude()                                     # cs-2's worktree is still there
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    branches.remove_worktree(d, root / ".changesets/p/cs-2/work")
    assert "/.changesets/" not in exclude()                                 # the last one is gone
    # what a closure leaves there after it (a branch that could not be kept: its tip.json) is still not the user's
    write(root, ".changesets/p/cs-2/tip.json", "{}\n")
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    git(root, "add", "-A")
    assert git(root, "diff", "--cached", "--name-only") == ""
    shutil.rmtree(root / ".changesets")
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""


def test_the_store_folder_ignores_itself_in_any_repository(tmp_path):
    """The store's root kept in git, with the project a repository of its own in it (a clone in the store): the
    worktrees under .changesets/ are not in the project's repository's working tree, so its info/exclude cannot
    hide them from the root's; the folder's own .gitignore does, for whatever is kept there."""
    root = tmp_path / "root"
    write(root, "README", "projects\n")
    write(root, ".gitignore", "/p/\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "store root")
    (root / "p").mkdir()
    git(root / "p", "init", "-q")
    d = project(root / "p")
    base = branches.snapshot(d, "Open cs-1", "user")
    shadow = root / ".changesets/p/cs-1"
    branches.add_worktree(d, shadow / "work", base, "undine/p/cs-1")
    branches.add_worktree(d, shadow / "base", base)
    write(shadow, "tip.json", "{}\n")
    assert (root / ".changesets/.gitignore").read_text() == "*\n"
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    git(root, "add", "-A")
    assert git(root, "diff", "--cached", "--name-only") == ""               # nothing of it staged
    branches.remove_worktree(d, shadow / "work")
    branches.remove_worktree(d, shadow / "base")
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""   # tip.json kept: still the store's
    write(root, ".changesets/.gitignore", "# the user's\n*\n")              # one there already: left as it is
    branches.add_worktree(d, root / ".changesets/p/cs-2/work", base, "undine/p/cs-2")
    assert (root / ".changesets/.gitignore").read_text() == "# the user's\n*\n"


def test_a_worktree_elsewhere_in_the_repository_excludes_itself(tmp_path):
    root = tmp_path / "root"
    write(root, "README", "projects\n")
    git(root, "init", "-q")
    d = project(root / "p", commit=False)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    w = root / "scratch/cs-1"                                               # no .changesets/ above it
    branches.add_worktree(d, w, branches.snapshot(d, "Open cs-1", "user"), "undine/p/cs-1")
    assert "/scratch/cs-1/" in (root / ".git/info/exclude").read_text().splitlines()
    assert git(root, "status", "--porcelain", "--untracked-files=all") == ""
    branches.remove_worktree(d, w)
    assert "/scratch/cs-1/" not in (root / ".git/info/exclude").read_text().splitlines()


# ---------------------------------------------------------------- the stat cache


def _aged(path: Path) -> None:
    """The file's time an hour back: its index entry is no racy one (one git reads again to be sure)."""
    t = path.stat().st_mtime - 3600
    os.utime(path, (t, t))


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file")
def test_a_file_unchanged_since_the_index_is_not_read_again(tmp_path):
    """snapshot starts from the user's index, commit_worktree from the worktree's, with their stat data: a file
    whose stat data is the index's is never opened. Made unreadable (core.trustctime off: the chmod changes its
    ctime), a commit that read it would fail."""
    d = project(tmp_path / "p")
    git(d, "config", "core.trustctime", "false")
    big = d / "libs/l/macro.bin"
    write(d, "libs/l/macro.bin", os.urandom(1 << 16))
    _aged(big)
    git(d, "add", "-A")
    git(d, "commit", "-qm", "macro")
    blob = git(d, "rev-parse", "HEAD:libs/l/macro.bin")
    write(d, SCH, edit(INV, *M1_W3))                                        # what changed is read
    big.chmod(0)
    try:
        base = branches.snapshot(d, "Open cs-1", "agent:claude")
    finally:
        big.chmod(0o644)
    assert git(d, "rev-parse", f"{base}:libs/l/macro.bin") == blob and "W=3" in show(d, base, SCH)

    work = branches.add_worktree(d, tmp_path / ".changesets/p/cs-1/work", base, "undine/p/cs-1")
    copy = work / "libs/l/macro.bin"
    _aged(copy)
    write(work, SCH, edit(edit(INV, *M1_W3), *M2_L))
    committed(work)                                     # its stat data is not the checkout's now: read once
    copy.chmod(0)
    try:
        assert branches.commit_worktree(work, "", "nothing", "agent:claude") is None       # not read again
        write(work, "libs/l/top.sch", TOP + "C {buf.sym} 400 0 0 0 {name=x2}\n")
        tip = committed(work)
    finally:
        copy.chmod(0o644)
    assert git(d, "rev-parse", f"{tip}:libs/l/macro.bin") == blob and parents(d, tip) != [base]
    assert git(work, "status", "--porcelain=v1") == ""                     # the worktree's index is the tip's


def test_the_second_commit_of_an_unchanged_big_file_does_not_read_it(tmp_path, monkeypatch):
    """Timing: git compresses a file over core.bigFileThreshold whenever it reads it into an index, its blob stored
    or not. Opening and committing a working copy where it did not change take a fraction of that."""
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.bigfilethreshold'='1m'")
    d = project(tmp_path / "p")
    big = d / "libs/l/macro.bin"
    write(d, "libs/l/macro.bin", os.urandom(24 << 20))
    _aged(big)
    git(d, "add", "-A")
    git(d, "commit", "-qm", "macro")

    def timed(fn, *a):
        start = time.perf_counter()
        out = fn(*a)
        return out, time.perf_counter() - start
    r = branches.repo(d)
    with branches._index(r, r.head) as env:                                 # no stat data: what reading costs
        _, read = timed(branches._project_tree, d, env)
    base, snap = timed(branches.snapshot, d, "Open cs-1", "agent:claude")
    work = branches.add_worktree(d, tmp_path / ".changesets/p/cs-1/work", base, "undine/p/cs-1")
    _aged(work / "libs/l/macro.bin")
    write(work, SCH, edit(INV, *M1_W3))
    committed(work)                                                         # read once: its time changed
    nothing, again = timed(branches.commit_worktree, work, "", "nothing", "agent:claude")
    write(work, SCH, edit(edit(INV, *M1_W3), *M2_L))
    tip, edited = timed(branches.commit_worktree, work, "", "size M2", "agent:claude")
    assert nothing is None and tip and "L=0.18" in show(d, tip, SCH)
    assert max(snap, again, edited) < read / 3, (read, snap, again, edited)


# ---------------------------------------------------------------- the plan and its write


def test_plan_merges_what_both_sides_changed_in_different_places(tmp_path):
    d = project(tmp_path / "p")
    base, work = opened(d, tmp_path)
    write(work, SCH, edit(INV, *M1_W3))                                     # theirs: M1, a box, top, a record
    layout(work / "libs/l", add_box("inv", 5000, 5000, 5500, 5500))
    layout(work / "libs/l", add_box("top", 0, 9000, 500, 9500))
    write(work, "history/l.jsonl", jsonl(rec(1, reason="size the inverter"), rec(2, actor="agent:claude")))
    tip = committed(work)
    write(d, SCH, edit(INV, *M2_L))                                         # ours: M2, another box, a record
    layout(d / "libs/l", add_box("inv", -5000, -5000, -4500, -4500))
    write(d, "history/l.jsonl", jsonl(rec(1, reason="size the inverter"), rec(2, cell="buf")))
    top_before = (d / "libs/l/top.oas").read_bytes()

    plan = branches.plan_merge(d, base, tip)
    assert plan["conflicts"] == [] and plan["kept"] == []
    assert plan["paths"] == ["history/l.jsonl", "libs/l/inv.oas", "libs/l/inv.sch", "libs/l/top.oas"]
    assert sorted(plan["writes"]) == plan["paths"]
    assert (d / "libs/l/top.oas").read_bytes() == top_before                # a plan writes nothing
    assert branches.write_plan(d, plan) == plan["paths"]
    sch = (d / SCH).read_text()
    assert comp(sch, "M1").props["W"] == "3" and comp(sch, "M2").props["L"] == "0.18"
    assert boxes(d / "libs/l/inv.oas", "inv") == ["box (-5000,-5000;-4500,-4500)", "box (0,0;500,2000)",
                                                   "box (5000,5000;5500,5500)"]
    assert boxes(d / "libs/l/top.oas", "top") == ["box (0,9000;500,9500)"]
    log = parse((d / "history/l.jsonl").read_bytes())
    assert [r["cell"] for r in log[:2]] == ["inv", "buf"] and log[2]["actor"] == "agent:claude"
    assert log[-1]["op"] == "merge"                                         # the log's own merge
    assert branches.plan_merge(d, base, tip)["writes"] == {}                 # merged: nothing left to write


def test_plan_conflicts_and_nothing_is_written(tmp_path):
    d = project(tmp_path / "p")
    base, work = opened(d, tmp_path)
    write(work, SCH, edit(INV, M2_AT, "{sky130_fd_pr/nfet_01v8.sym} 20 60 0 1"))
    (work / "libs/l/buf.sch").unlink()                                     # theirs deletes, ours changes
    write(work, "libs/l/top.sch", TOP + "C {buf.sym} 400 0 0 0 {name=x2}\n")    # theirs changes, ours deletes
    tip = committed(work)
    write(d, SCH, edit(INV, M2_AT, "{sky130_fd_pr/nfet_01v8.sym} -20 80 0 0"))
    write(d, "libs/l/buf.sch", BUF + "C {inv.sym} 400 0 0 0 {name=x3}\n")
    (d / "libs/l/top.sch").unlink()
    before = tree_state(d)

    plan = branches.plan_merge(d, base, tip)
    got = {c["path"]: (c["kind"], [x["id"] for x in c["conflicts"]]) for c in plan["conflicts"]}
    assert got == {SCH: ("schematic", ["inst:M2:placement"]), "libs/l/buf.sch": ("schematic", ["file"]),
                   "libs/l/top.sch": ("schematic", ["file"])}
    sch = plan["conflicts"][1]["conflicts"][0]
    assert sch["what"] == "instance M2: placement" and sch["ours"]["y"] == 80 and sch["theirs"]["x"] == 20
    buf = plan["conflicts"][0]["conflicts"][0]
    assert (buf["base"], buf["ours"], buf["theirs"]) == ("file", "changed", "deleted") and "theirs" in buf["what"]
    top = plan["conflicts"][2]["conflicts"][0]
    assert (top["ours"], top["theirs"]) == ("deleted", "changed")
    with pytest.raises(ValueError, match="conflicts"):
        branches.write_plan(d, plan)
    assert tree_state(d) == before


def test_plan_of_files_added_deleted_and_merged_by_lines(tmp_path):
    d = project(tmp_path / "p")
    write(d, "README.md", "one\ntwo\nthree\nfour\nfive\n")
    base, work = opened(d, tmp_path)
    write(work, "libs/l/new.sch", HEADER)                                   # added
    (work / "libs/l/buf.sch").unlink()                                     # deleted, ours untouched
    write(work, "notes.txt", "theirs\n")                                   # added on both sides, differently
    write(work, "same.txt", "same\n")                                      # added on both sides, the same
    write(work, "README.md", "ONE\ntwo\nthree\nfour\nfive\n")              # lines: both, apart
    tip = committed(work)
    write(d, "notes.txt", "ours\n")
    write(d, "same.txt", "same\n")
    write(d, "README.md", "one\ntwo\nthree\nfour\nFIVE\n")

    plan = branches.plan_merge(d, base, tip)
    assert plan["conflicts"] == [{"path": "notes.txt", "kind": "text", "conflicts": [
        {"id": "lines", "what": "1 place changed differently on both sides (merged by lines)", "base": None,
         "ours": "added", "theirs": "added", "taken": "ours", "resolved": False}]}]
    write(d, "notes.txt", "theirs\n")                                      # settled by hand
    plan = branches.plan_merge(d, base, tip)
    assert plan["conflicts"] == []
    assert plan["paths"] == ["README.md", "libs/l/buf.sch", "libs/l/new.sch", "notes.txt", "same.txt"]
    assert plan["writes"] == {"README.md": b"ONE\ntwo\nthree\nfour\nFIVE\n", "libs/l/buf.sch": None,
                              "libs/l/new.sch": HEADER.encode()}
    branches.write_plan(d, plan)
    assert not (d / "libs/l/buf.sch").exists() and (d / "libs/l/new.sch").read_text() == HEADER
    assert (d / "README.md").read_text() == "ONE\ntwo\nthree\nfour\nFIVE\n"


def test_a_binary_file_changed_on_both_sides_is_a_conflict(tmp_path):
    d = project(tmp_path / "p")
    write(d, "doc/chip.png", b"\x89PNG\0base")
    base, work = opened(d, tmp_path)
    write(work, "doc/chip.png", b"\x89PNG\0theirs")
    tip = committed(work)
    write(d, "doc/chip.png", b"\x89PNG\0ours")
    [c] = branches.plan_merge(d, base, tip)["conflicts"]
    assert c["path"] == "doc/chip.png" and c["kind"] == "text" and c["conflicts"][0]["id"] == "error"
    (d / "doc/chip.png").unlink()                                          # a directory in the file's place
    write(d, "doc/chip.png/x", "x")
    [c] = branches.plan_merge(d, base, tip)["conflicts"]
    assert c["conflicts"][0]["id"] == "file" and c["conflicts"][0]["what"] == "not a file in the project"


def test_revert_keeps_the_files_the_changeset_added(tmp_path):
    d = project(tmp_path / "p")
    base, work = opened(d, tmp_path)
    write(work, SCH, edit(INV, *M1_W3))
    write(work, "libs/l/ota.sch", HEADER)
    tip = committed(work)
    branches.write_plan(d, branches.plan_merge(d, base, tip))               # applied
    assert (d / "libs/l/ota.sch").is_file() and "W=3" in (d / SCH).read_text()

    plan = branches.plan_merge(d, tip, base, keep_added=True)              # reverted: the tip is the base
    assert plan["kept"] == ["libs/l/ota.sch"] and plan["paths"] == [SCH] and plan["writes"] == {SCH: INV.encode()}
    assert branches.plan_merge(d, tip, base)["writes"] == {SCH: INV.encode(), "libs/l/ota.sch": None}
    branches.write_plan(d, plan)
    assert (d / SCH).read_text() == INV and (d / "libs/l/ota.sch").read_text() == HEADER

    write(d, SCH, edit(INV, *M2_L))                                         # applied again, then edited after
    branches.write_plan(d, branches.plan_merge(d, base, tip))
    write(d, SCH, edit((d / SCH).read_text(), "W=3", "W=5"))             # the merge's canonical text
    [c] = branches.plan_merge(d, tip, base, keep_added=True)["conflicts"]    # a later edit of the same value
    assert c["path"] == SCH and [x["id"] for x in c["conflicts"]] == ["inst:M1:prop:W"]


@pytest.mark.parametrize("fail_at", range(1, 7))
def test_write_plan_puts_everything_back_on_a_failure(tmp_path, monkeypatch, fail_at):
    d = project(tmp_path / "p")
    plan = {"writes": {SCH: b"new inv", "libs/l/inv.sym": b"new sym", "libs/l/buf.sch": None,
                       "libs/m/sub/added.sch": HEADER.encode()}, "conflicts": []}
    before = tree_state(d)
    calls = []

    def failing(src: Path, dst: Path) -> None:                             # renames: buf aside; inv aside, in;
        calls.append(dst)                                                   # sym aside, in; added in
        if len(calls) == fail_at:
            raise OSError("disk full")
        os.replace(src, dst)
    monkeypatch.setattr(branches, "_replace", failing)
    with pytest.raises(OSError, match="disk full"):
        branches.write_plan(d, plan)
    assert tree_state(d) == before and not (d / "libs/m").exists()        # no file, no stray temporary, no dir


def test_write_plan_writes_new_files_with_the_umask_mode(tmp_path):
    d = project(tmp_path / "p")
    os.chmod(d / "libs/l/inv.sym", 0o755)
    branches.write_plan(d, {"writes": {"libs/l/inv.sym": b"x", "libs/l/n.sch": b"y"}, "conflicts": []})
    umask = os.umask(0)
    os.umask(umask)
    assert (d / "libs/l/inv.sym").stat().st_mode & 0o777 == 0o755
    assert (d / "libs/l/n.sch").stat().st_mode & 0o777 == 0o666 & ~umask
    with pytest.raises(ValueError, match="inside the project"):
        branches.write_plan(d, {"writes": {"../x": b""}, "conflicts": []})


# ---------------------------------------------------------------- the commit on the user's branch


def test_is_clean(tmp_path):
    d = project(tmp_path / "p")
    assert branches.is_clean(d)
    for rel in OWN:                                                         # Undine's output, the records
        write(d, rel, "own")
    assert branches.is_clean(d)
    write(d, "libs/l/new.sch", HEADER)                                      # untracked
    assert not branches.is_clean(d)
    (d / "libs/l/new.sch").unlink()
    write(d, SCH, edit(INV, *M2_L))                                         # modified
    assert not branches.is_clean(d)
    git(d, "add", SCH)                                                      # staged
    assert not branches.is_clean(d)
    git(d, "reset", "-q", "--hard")
    (d / ".git/MERGE_HEAD").write_text(head(d) + "\n")                      # a merge in progress
    assert not branches.is_clean(d)
    (d / ".git/MERGE_HEAD").unlink()
    assert branches.is_clean(d)

    repo = tmp_path / "repo"                                                # staged outside the project
    write(repo, "README", "x\n")
    git(repo, "init", "-q")
    sub = project(repo / "designs/p", commit=False)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    assert branches.is_clean(sub)
    write(repo, "README", "y\n")
    assert branches.is_clean(sub)                                           # not the project's
    git(repo, "add", "README")
    assert not branches.is_clean(sub)

    unborn = project(tmp_path / "u", commit=False)
    assert not branches.is_clean(unborn)


def test_commit_merge_commits_those_paths_and_resets_their_index_entries(tmp_path):
    d = project(tmp_path / "p")
    base, work = opened(d, tmp_path)
    write(work, SCH, edit(INV, *M1_W3))
    write(work, "libs/l/ota.sch", HEADER)
    (work / "libs/l/buf.sch").unlink()
    tip = committed(work)
    for rel in OWN:
        write(d, rel, "own")
    assert branches.is_clean(d)
    first, branch = head(d), git(d, "symbolic-ref", "HEAD")
    plan = branches.plan_merge(d, base, tip)
    branches.write_plan(d, plan)
    sha = branches.commit_merge(d, [first, tip], "Apply cs-1: size M1\n\nwhy", "user", plan["paths"])
    assert stat_known(d, SCH) and stat_known(d, "libs/l/ota.sch")         # first: git reads them, and keeps it
    assert head(d) == sha and git(d, "symbolic-ref", "HEAD") == branch and parents(d, sha) == [first, tip]
    assert git(d, "diff", "--name-only", "--no-renames", first, sha).splitlines() == \
        ["libs/l/buf.sch", SCH, "libs/l/ota.sch"]
    assert git(d, "log", "-1", "--format=%an|%cn|%s", sha) == "user|Undine|Apply cs-1: size M1"
    assert git(d, "diff", "--cached", "--name-only") == "" and git(d, "diff", "--name-only") == ""
    assert branches.is_clean(d) and "changesets/cs-1.json" not in files(d, sha)

    # not clean: commit_merge takes those paths only, what else is modified or staged stays as it is
    write(d, "libs/l/top.sch", TOP + "* staged\n")
    git(d, "add", "libs/l/top.sch")
    write(d, "libs/l/inv.sym", INV_SYM + "* modified\n")
    write(d, SCH, edit(INV, *M2_L))
    assert not branches.is_clean(d)
    second = branches.commit_merge(d, [sha], "Revert cs-2", "user", [SCH])
    assert git(d, "diff", "--name-only", sha, second) == SCH and parents(d, second) == [sha]
    assert git(d, "diff", "--cached", "--name-only") == "libs/l/top.sch"
    assert git(d, "diff", "--name-only") == "libs/l/inv.sym"

    with pytest.raises(ValueError, match="moved"):                          # HEAD is not its first parent
        branches.commit_merge(d, [sha, tip], "Apply cs-1", "user", [SCH])
    assert head(d) == second

    git(d, "checkout", "-q", "--detach")                                   # a detached HEAD moves alone
    write(d, "libs/l/inv.sym", INV_SYM)
    third = branches.commit_merge(d, [second], "Apply cs-3", "user", ["libs/l/inv.sym"])
    assert head(d) == third and git(d, "rev-parse", branch) == second


def stat_known(d: Path, rel: str) -> bool:
    """Whether the index entry of `rel` has its stat data (a reset -q leaves none: git status reads the file again)."""
    return "mtime: 0:0" not in git(d, "ls-files", "--debug", "--", rel)


def test_first_commit_is_the_project_as_it_is_on_an_unborn_branch_only(tmp_path):
    """git init and git-setup commit nothing, and an apply onto a branch with no commit is left uncommitted:
    first_commit commits the project's files as a base would have them, the index then its own."""
    d = project(tmp_path / "p", setup=False, commit=False)
    assert branches.ensure_repo(d) == {"git_setup": True}
    for rel in OWN:                                                         # Undine's output, the records
        write(d, rel, "own")
    write(d, "notes.txt", "untracked, not ignored\n")
    write(d, ".gitignore", (d / ".gitignore").read_text() + "*.bak\n")
    write(d, "libs/l/inv.sch.bak", "ignored\n")
    branch = git(d, "symbolic-ref", "HEAD")
    assert not branches.is_clean(d)
    libraries(d, shared="libs/shared")                                     # a library no worktree can hold: refused
    os.symlink(tmp_path, d / "libs/shared")
    with pytest.raises(branches.Unsupported, match="^library shared: libs/shared is a symbolic link"):
        branches.first_commit(d, "first", "user")
    assert branches.repo(d).head is None and git(d, "ls-files") == ""
    os.unlink(d / "libs/shared")
    libraries(d)
    made = branches.first_commit(d, branches.first_message("p", setup=True), "agent:claude")
    sha = made["commit"]
    assert made["note"] is None and stat_known(d, SCH) and stat_known(d, "libs/l/inv.oas")    # first: git reads it
    assert head(d) == sha and git(d, "symbolic-ref", "HEAD") == branch and parents(d, sha) == []
    fs = files(d, sha)
    assert {SCH, "libs/l/inv.oas", "history/l.jsonl", "project.json", ".gitattributes", ".gitignore",
            "notes.txt"} <= fs and "libs/l/inv.sch.bak" not in fs
    assert not any(f.startswith(("runs/", "tapeout/", "llm/", ".undine/", "changesets/")) or f.endswith(".pre-split")
                   for f in fs)
    assert git(d, "log", "-1", "--format=%an <%ae>|%cn|%s", sha) == \
        "agent:claude <undine@localhost>|Undine|Project p: its files when Undine set up its repository"
    assert "the changeset records (changesets/)" in git(d, "log", "-1", "--format=%b", sha)
    # the index is the commit's: `git status` shows nothing but what the tree leaves out and git does not ignore
    assert git(d, "diff", "--cached", "--name-only") == "" and git(d, "diff", "--name-only") == ""
    assert git(d, "status", "--porcelain", "--untracked-files=all").splitlines() == ["?? changesets/cs-1.json"]
    assert branches.is_clean(d)                                             # an apply is committed now
    assert not list((d / ".git").glob("undine-index-*"))
    assert branches.first_commit(d, "again", "user") is None and head(d) == sha     # HEAD has a commit: nothing


def test_first_commit_keeps_what_the_user_tracks_and_untracks_undine_output(tmp_path):
    """The user's index of an unborn branch: what they track of the project is committed as it is now (a file
    .gitignore ignores, added with -f, kept; one staged then deleted, not), and Undine's output and the records they
    staged (git add -A before git-setup wrote .gitignore) are untracked again; staged or not, they never keep a
    merge from being committed."""
    d = project(tmp_path / "p", setup=False, commit=False)
    for rel in OWN:
        write(d, rel, "own")
    write(d, "gone.txt", "staged, then deleted\n")
    git(d, "init", "-q")
    git(d, "add", "-A")                                                     # runs/, changesets/ ... staged
    (d / "gone.txt").unlink()
    git_setup(d, say=None)
    write(d, ".gitignore", (d / ".gitignore").read_text() + "*.bak\n")
    write(d, "keep.bak", "tracked although ignored\n")
    git(d, "add", "-f", "keep.bak")
    write(d, "keep.bak", "tracked although ignored, changed since\n")
    assert not branches.is_clean(d)
    sha = branches.first_commit(d, branches.first_message("p"), "user")["commit"]
    fs = files(d, sha)
    assert "keep.bak" in fs and show(d, sha, "keep.bak") == "tracked although ignored, changed since"
    assert "gone.txt" not in fs and not [f for f in fs if f.startswith(("runs/", "changesets/", "tapeout/", "llm/"))]
    assert git(d, "diff", "--cached", "--name-only") == ""                 # nothing of Undine's staged any more
    assert git(d, "status", "--porcelain").splitlines() == ["?? changesets/"]
    assert branches.is_clean(d)
    git(d, "add", "-f", "runs/j1/status.json", "changesets/cs-1.json")     # staged again: still not the user's changes
    assert branches.is_clean(d)
    write(d, "notes.txt", "the user's\n")
    git(d, "add", "notes.txt")
    assert not branches.is_clean(d)


def test_first_commit_refuses_unsaved_layout_edits(tmp_path):
    """A log whose journal holds layout edits not saved to the cell files (the pre-commit hook refuses that commit;
    commit-tree runs no hook): nothing is committed, the layouts are named; once saved, the first commit is made."""
    d = project(tmp_path / "p", setup=False, commit=False)
    git_setup(d, say=None)
    write(d, "history/l.jsonl", jsonl(rec(1, reason="size the inverter"), rec(2, op="add_shape", view="layout"),
                                      rec(3, op="add_shape", cell="top", view="layout")))
    with pytest.raises(branches.Unsaved, match="^library l has layout edits not saved to its cell files "
                                               r"\(cells inv, top\): save the layouts") as e:
        branches.first_commit(d, "first", "user")
    assert e.value.logs == [{"path": "history/l.jsonl", "lib": "l", "cells": ["inv", "top"]}]
    assert e.value.code == "unsaved" and branches.repo(d).head is None
    write(d, "history/l.jsonl", (d / "history/l.jsonl").read_text() +
          jsonl(rec(4, op="save", cell=None, view="layout")).decode())       # saved: the journal ends
    assert branches.first_commit(d, "first", "user")["commit"] == head(d)


def test_first_commit_makes_the_branch_only_while_it_is_unborn(tmp_path, monkeypatch):
    """A commit of the user's made while the first commit is built wins: the branch is made only from nothing, and
    first_commit then says the branch has a commit (None), as it would have before."""
    d = project(tmp_path / "p", setup=False, commit=False)
    branches.ensure_repo(d)
    real = branches._commit

    def meanwhile(*a, **k):
        git(d, "add", "project.json")
        git(d, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "the user's")
        return real(*a, **k)

    monkeypatch.setattr(branches, "_commit", meanwhile)
    assert branches.first_commit(d, "first", "user") is None
    assert git(d, "log", "--format=%s") == "the user's"


def test_first_commit_with_the_index_held_by_another_git_says_what_to_run(tmp_path, monkeypatch):
    """The commit is made; the index, which another git holds (index.lock) longer than first_commit waits, is not
    reset to it: the note says so, and what to run."""
    d = project(tmp_path / "p", setup=False, commit=False)
    branches.ensure_repo(d)
    monkeypatch.setattr(branches, "_TRIES", 2)
    (d / ".git/index.lock").touch()
    made = branches.first_commit(d, "first", "user")
    assert made["commit"] == head(d) and "index.lock" in made["note"] and f"run `git reset` in {d}" in made["note"]
    (d / ".git/index.lock").unlink()
    assert not branches.is_clean(d)                                         # the index is still empty
    git(d, "reset", "-q")
    assert branches.is_clean(d)


def test_first_commit_of_a_project_in_a_subdirectory_commits_its_directory_only(tmp_path):
    repo = tmp_path / "repo"
    write(repo, "README", "designs\n")
    git(repo, "init", "-q")
    git(repo, "add", "README")                                             # the user's, staged: left so
    d = project(repo / "designs/p", commit=False)                          # git-setup: the repository it is in
    write(repo, "other/x", "another project's\n")
    sha = branches.first_commit(d, branches.first_message("p"), "user")["commit"]
    fs = files(repo, sha)
    assert git(repo, "rev-parse", "HEAD") == sha and f"designs/p/{SCH}" in fs
    assert all(f.startswith("designs/p/") for f in fs)
    assert git(repo, "status", "--porcelain").splitlines() == ["A  README", "?? other/"]    # as they were
    assert git(repo, "log", "-1", "--format=%s", sha) == "Project p: its first commit, its files as they were"


# ---------------------------------------------------------------- no hook, no identity


HOOKS = ("pre-commit", "commit-msg", "post-commit", "post-checkout", "reference-transaction", "post-index-change",
         "pre-auto-gc", "post-rewrite")


def _everything(d: Path, tmp_path: Path, cid: str) -> None:
    """Every operation of the module, as the backend runs them: open, edit, commit, apply, close."""
    base, work = opened(d, tmp_path, cid)
    branches.add_worktree(d, tmp_path / f".changesets/p/{cid}/base", base)
    write(work, SCH, (work / SCH).read_text() + "* " + cid + "\n")
    tip = committed(work)
    assert branches.is_clean(d)
    plan = branches.plan_merge(d, base, tip)
    branches.write_plan(d, plan)
    branches.commit_merge(d, [head(d), tip], f"Apply {cid}", "user", plan["paths"])
    for side in ("work", "base"):
        branches.remove_worktree(d, tmp_path / f".changesets/p/{cid}/{side}")
    branches.keep_ref(d, f"undine/p/{cid}", f"refs/undine/changesets/p/{cid}")
    branches.prune(d)


def test_no_hook_runs(tmp_path):
    d = project(tmp_path / "p")
    ran = tmp_path / "ran"
    hooks = tmp_path / "hooks"
    for name in HOOKS:
        for where in (d / ".git/hooks", hooks):
            write(where, name, f"#!/bin/sh\necho {name} >> '{ran}'\nexit 1\n")
            os.chmod(where / name, 0o755)
    _everything(d, tmp_path, "cs-1")
    git(d, "config", "core.hooksPath", str(hooks))                          # the user's hooks elsewhere
    _everything(d, tmp_path, "cs-2")
    assert not ran.exists()
    git(d, "update-ref", "refs/x", "HEAD", ok=False)                        # they are live for git itself
    assert "reference-transaction" in ran.read_text()


def test_no_git_identity_is_needed(tmp_path, monkeypatch):
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    d = project(tmp_path / "p", setup=False, commit=False)
    assert branches.ensure_repo(d) == {"git_setup": True}
    git(d, "config", "user.useConfigOnly", "true")                         # git guesses no identity either
    git(d, "add", "-A")
    assert subprocess.run(["git", "commit", "-qm", "x"], cwd=d, capture_output=True).returncode != 0
    paths = git(d, "ls-files").splitlines()
    git(d, "reset", "-q")
    first = branches.commit_merge(d, [], "Initial", "user", paths)          # on the unborn branch
    assert parents(d, first) == [] and git(d, "status", "--porcelain") == ""
    _everything(d, tmp_path, "cs-1")
    assert git(d, "log", "--format=%an|%cn", "-2", "--no-walk=unsorted", "HEAD", "HEAD^2") == \
        "user|Undine\nagent:claude|Undine"


def test_identity():
    assert branches.identity("agent:claude") == {"GIT_AUTHOR_NAME": "agent:claude",
                                                 "GIT_AUTHOR_EMAIL": "undine@localhost",
                                                 "GIT_COMMITTER_NAME": "Undine",
                                                 "GIT_COMMITTER_EMAIL": "undine@localhost"}
    assert branches.identity("")["GIT_AUTHOR_NAME"] == "user"
    assert branches.identity("a <b>\nc")["GIT_AUTHOR_NAME"] == "a b c"


# ---------------------------------------------------------------- the diff of a base


def test_diff_of_the_base_against_the_working_copy_and_the_tip(tmp_path):
    d = project(tmp_path / "p")
    base, work = opened(d, tmp_path)
    write(work, SCH, edit(INV, *M1_W3))
    write(work, "libs/l/ota.sch", HEADER)
    (work / "libs/l/buf.sch").unlink()
    write(work, "changesets/cs-1.json", "{}")
    want = {("l", "inv", "schematic"): "modified", ("l", "ota", "schematic"): "added",
            ("l", "buf", "schematic"): "deleted"}
    live = branches.diff(work, base)
    assert live["base"] == base and live["rev"] is None and views(live) == want and live["other"] == []
    inv = next(v for c in live["cells"] if c["cell"] == "inv" for v in c["views"])
    assert [c["id"] for c in inv["diff"]["changes"]] == ["inst:M1:prop:W"]
    tip = committed(work)
    at = branches.diff(work, base, tip)
    assert at["rev"] == tip and views(at) == want
    assert views(branches.diff(work, base, lib="l", cell="inv")) == {("l", "inv", "schematic"): "modified"}
    assert views(branches.diff(d, base)) == {}                              # the project, unchanged since


def test_diff_of_a_base_with_files_the_index_has_not(tmp_path):
    """A base made from a working tree has its untracked files: unchanged since, they are no change."""
    d = project(tmp_path / "p")
    write(d, "libs/l/extra.sch", HEADER)
    write(d, "notes.txt", "x\n")
    base = branches.snapshot(d, "Open cs-1", "user")
    assert views(branches.diff(d, base)) == {} and branches.diff(d, base)["other"] == []
    write(d, "libs/l/extra.sch", BUF)
    write(d, "libs/l/later.sch", HEADER)
    (d / "notes.txt").unlink()
    got = branches.diff(d, base)
    assert views(got) == {("l", "extra", "schematic"): "modified", ("l", "later", "schematic"): "added"}
    assert got["other"] == [{"path": "notes.txt", "status": "deleted"}]
