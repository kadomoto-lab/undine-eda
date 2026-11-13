"""The git history of a project over HTTP (vcs/history.py, api.py /api/p/{project}/git...): the working tree's cells
against HEAD, the commits by cell, the semantic diff of a commit or of the working tree, the check of a merge commit,
git-setup (the first commit after its git init), and the open layouts with unsaved edits; with real git in temporary
directories."""
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import klayout.db as kdb
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.layouts import layoutstore  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store.changeset import Changesets  # noqa: E402
from undine.vcs import history  # noqa: E402
from undine.vcs.gitmerge import GITIGNORE  # noqa: E402
from test_vcs_schematic import INV, INV_SYM, edit  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

MET1 = (68, 20)
HEADER = "v {xschem version=3.4.5 file_version=1.2\n}\nG {}\nK {}\nV {}\nS {}\nE {}\n"
BUF = HEADER + "C {inv.sym} 0 0 0 0 {name=x1}\nC {inv.sym} 200 0 0 0 {name=x2}\n"     # above inv


@pytest.fixture(autouse=True)
def git_env(monkeypatch):
    """git (and the merge driver it runs) without the user's or the system's configuration."""
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def git(d: Path, *a: str, ok: bool = True) -> str:
    r = subprocess.run(["git", *a], cwd=d, capture_output=True, text=True, errors="surrogateescape")
    assert not ok or r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def write(d: Path, rel: str, data: str | bytes) -> None:
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)


def layout(lib: Path, *extra: tuple[int, int, int, int]) -> None:
    """inv (a metal box, a via1 instance) and via1, one file per cell; `extra` boxes added to inv's metal, written
    the way the layout store saves (only the cells that changed)."""
    ly = kdb.Layout()
    if not extra:
        ly.dbu = 0.001
        inv, via = ly.create_cell("inv"), ly.create_cell("via1")
        via.shapes(ly.layer(67, 44)).insert(kdb.Box(0, 0, 170, 170))
        inv.shapes(ly.layer(*MET1)).insert(kdb.Box(0, 0, 500, 2000))
        inv.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans(100, 100)))
        layoutstore.write(lib, ly, {})
        return
    layoutstore.read(lib, ly)
    saved = layoutstore.snapshot(ly)
    for box in extra:
        ly.cell("inv").shapes(ly.layer(*MET1)).insert(kdb.Box(*box))
    layoutstore.write(lib, ly, saved)


def commit(d: Path, msg: str) -> str:
    git(d, "add", "-A", ".")
    git(d, "commit", "-qm", msg)
    return git(d, "rev-parse", "HEAD")


@pytest.fixture
def api(tmp_path):
    """Project p with library l: inv (schematic, symbol, layout with the child cell via1) and buf above it; no git."""
    root = tmp_path / "projects"
    srv = Server(root, {}, [tmp_path], llm_policy="none")
    prj = srv.store.create("p")
    prj.create_library("l")
    d = prj.path
    write(d, "libs/l/inv.sch", INV)
    write(d, "libs/l/inv.sym", INV_SYM)
    write(d, "libs/l/buf.sch", BUF)
    layout(d / "libs/l")
    return SimpleNamespace(srv=srv, client=TestClient(create_app(srv, None)), d=d, root=root)


def get(api, url: str, status: int = 200) -> dict:
    r = api.client.get(url)
    assert r.status_code == status, r.text
    return r.json()


def setup(api, project: str = "p", init: bool = True) -> dict:
    """POST .../git/setup. `init`: the repository made by hand first (git init), which git-setup leaves with no commit,
    as most tests commit their own base; without, git-setup's own git init, and its first commit, in a project in no
    repository."""
    if init and project == "p":
        git(api.d, "init", "-q")
    r = api.client.post(f"/api/p/{project}/git/setup")
    assert r.status_code == 200, r.text
    return r.json()


def views(st: dict) -> dict[tuple[str, str, str], str]:
    """{(lib, cell, view): status} of a status' or a commit's cells."""
    return {(c["lib"], c["cell"], v["view"]): v["status"] for c in st["cells"] for v in c["views"]}


def others(st: dict) -> dict[str, str]:
    return {o["path"]: o["status"] for o in st["other"]}


# ---------------------------------------------------------------- status and setup


def test_setup_makes_a_repository_and_status_lists_the_cells(api):
    st = get(api, "/api/p/p/git")
    assert st == {"repo": False, "preview": False, "setup": None, "prefix": None, "branch": None, "head": None,
                  "operation": None, "conflicted": [], "cells": [], "other": [], "unsaved": [], "stale": [],
                  "reads": {}}
    assert get(api, "/api/p/p/git/log", 400)["detail"].startswith("p is not in a git repository")
    assert api.client.get("/api/p/p/git/diff?rev=worktree").status_code == 400

    # its own git init: the project's files are its first commit (an apply is committed only onto a commit)
    st = setup(api, init=False)
    first = "Project p: its files when Undine set up its repository"
    assert st["repo"] is True and st["hook_note"] is None and st["commit"] == st["head"]["sha"]
    assert st["head"]["subject"] == first and st["head"]["author"] == "user" and st["cells"] == st["other"] == []
    log = get(api, "/api/p/p/git/log")["commits"]
    assert [c["subject"] for c in log] == [first] and set(views(log[0]).values()) == {"added"}
    assert views(log[0])[("l", "via1", "layout")] == "added"
    assert setup(api, init=False)["commit"] is None                       # again: no git init, no commit

    # a repository made by hand (git init): git-setup commits nothing, and the status lists the cells as untracked
    shutil.rmtree(api.d / ".git")
    st = setup(api)
    assert st["repo"] is True and st["setup"] == {"merge": True, "diff": True, "hook": True, "gitignore": []}
    assert st["hook_note"] is None and st["prefix"] == "" and st["branch"] and st["head"] is None and st["operation"] is None
    assert views(st) == {("l", "buf", "schematic"): "untracked", ("l", "inv", "schematic"): "untracked",
                         ("l", "inv", "symbol"): "untracked", ("l", "inv", "layout"): "untracked",
                         ("l", "via1", "layout"): "untracked"}
    inv = next(c for c in st["cells"] if c["cell"] == "inv")
    assert [v["path"] for v in inv["views"]] == ["libs/l/inv.sch", "libs/l/inv.sym", "libs/l/inv.oas"]
    assert others(st) == {".gitattributes": "untracked", ".gitignore": "untracked", "project.json": "untracked",
                          "undine.lock": "untracked"}
    # no commit yet: no log, and the working tree against the empty tree
    assert get(api, "/api/p/p/git/log") == {"commits": [], "more": False}
    d = get(api, "/api/p/p/git/diff?rev=worktree")
    assert d["base"] is None and d["rev"] is None
    sch = next(v for c in d["cells"] if c["cell"] == "inv" for v in c["views"] if v["view"] == "schematic")
    assert sch["diff"]["kind"] == "schematic" and any(c["id"] == "inst:M1" and c["do"] == "add"
                                                      for c in sch["diff"]["changes"])
    # staged files of the first commit are added
    git(api.d, "add", "libs/l/inv.sch")
    assert views(get(api, "/api/p/p/git"))[("l", "inv", "schematic")] == "added"
    assert setup(api)["setup"] == st["setup"]              # again: nothing changes


@pytest.mark.parametrize("theirs", ["hook", "hooksPath"])
def test_setup_says_why_the_hook_was_not_installed(api, theirs):
    """A repository with a pre-commit hook of its own (or core.hooksPath): git-setup leaves it alone, and says what to
    add to it rather than nothing."""
    git(api.d, "init", "-q")
    if theirs == "hook":
        hook = api.d / ".git/hooks/pre-commit"
        write(api.d, ".git/hooks/pre-commit", "#!/bin/sh\nexec pre-commit run\n")
        hook.chmod(0o755)
    else:
        git(api.d, "config", "core.hooksPath", ".githooks")
    for _ in range(2):                                          # again: the same, never a silent success
        st = setup(api)
        assert st["setup"] == {"merge": True, "diff": True, "hook": False, "gitignore": []}
        assert st["hook_note"].startswith("the pre-commit hook is not installed (")
        assert ("is not Undine's" if theirs == "hook" else "core.hooksPath is .githooks") in st["hook_note"]
        assert "-m undine precommit" in st["hook_note"] or " precommit" in st["hook_note"]
    if theirs == "hook":
        assert (api.d / ".git/hooks/pre-commit").read_text() == "#!/bin/sh\nexec pre-commit run\n"


def test_setup_adds_the_gitignore_lines_a_project_set_up_before_lacks(api):
    """A project set up before a line was added to Undine's .gitignore (sar11: /changesets/requests/, so git listed the
    request log as untracked) says which lines its .gitignore lacks; only git-setup adds them, keeping the user's."""
    setup(api)
    ignore = api.d / ".gitignore"
    mine = ignore.read_text().replace("/changesets/requests/\n", "") + "*.log\n"
    ignore.write_text(mine)
    write(api.d, "changesets/requests/r1.json", "{}")
    st = get(api, "/api/p/p/git")
    assert st["setup"] == {"merge": True, "diff": True, "hook": True, "gitignore": ["/changesets/requests/"]}
    assert ignore.read_text() == mine                                     # reading the status changes nothing
    assert "changesets/" in git(api.d, "status", "--porcelain")
    st = setup(api, init=False)
    assert st["gitignore_added"] == ["/changesets/requests/"] and st["setup"]["gitignore"] == []
    assert ignore.read_text() == mine + "/changesets/requests/\n"       # appended, the user's lines kept
    assert "changesets/" not in git(api.d, "status", "--porcelain")
    assert setup(api, init=False)["gitignore_added"] == []


def test_status_by_cell_against_head(api):
    setup(api)
    head = commit(api.d, "base")
    write(api.d, "libs/l/inv.sch", edit(INV, "W=2", "W=4"))      # modified
    (api.d / "libs/l/inv.sym").unlink()                           # deleted
    write(api.d, "libs/l/nand.sch", HEADER)                       # untracked
    write(api.d, "libs/l/nand.sym", INV_SYM)
    git(api.d, "add", "libs/l/nand.sym")                          # added
    write(api.d, "libs/l/nand.intent.yaml", "notes: ''\n")
    write(api.d, "libs/l/nand.netlist.yaml", "ports: []\n")
    write(api.d, "libs/l/nand.sv", "module nand; endmodule\n")
    write(api.d, "libs/l/notes.txt", "x\n")                       # in a library, of no cell
    write(api.d, "libs/l/sub/x.sch", HEADER)                      # in a library's subdirectory
    write(api.d, "libs/old/gone.sym", INV_SYM)                    # libs/<lib>/ that project.json does not name
    write(api.d, "history/l.jsonl", "{}\n")
    write(api.d, "runs/r1/out.log", "ignored\n")                  # .gitignore
    prj = json.loads((api.d / "project.json").read_text())
    prj["note"] = "x"
    write(api.d, "project.json", json.dumps(prj))
    st = get(api, "/api/p/p/git")
    assert st["head"]["sha"] == head and st["head"]["subject"] == "base" and st["head"]["parents"] == []
    assert st["branch"] in st["head"]["refs"]
    assert views(st) == {("l", "inv", "schematic"): "modified", ("l", "inv", "symbol"): "deleted",
                         ("l", "nand", "schematic"): "untracked", ("l", "nand", "symbol"): "added",
                         ("l", "nand", "intent"): "untracked", ("l", "nand", "netlist"): "untracked",
                         ("l", "nand", "rtl"): "untracked", ("old", "gone", "symbol"): "untracked"}
    nand = next(c for c in st["cells"] if c["cell"] == "nand")
    assert [v["view"] for v in nand["views"]] == ["schematic", "symbol", "netlist", "intent", "rtl"]
    assert others(st) == {"history/l.jsonl": "untracked", "libs/l/notes.txt": "untracked",
                          "libs/l/sub/x.sch": "untracked", "project.json": "modified"}
    assert st["conflicted"] == [] and st["unsaved"] == []


def test_detached_head_and_non_utf8_names(api):
    setup(api)
    head = commit(api.d, "base")
    git(api.d, "checkout", "-q", "--detach")
    cafe = (api.d / "libs/l").joinpath(os.fsdecode(b"caf\xe9.sch"))
    cafe.write_text(HEADER)
    r = api.client.get("/api/p/p/git")
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["branch"] is None and st["head"]["sha"] == head
    assert views(st) == {("l", "caf\\xe9", "schematic"): "untracked"}
    d = get(api, "/api/p/p/git/diff?rev=worktree")
    assert d["cells"][0]["views"][0]["path"] == "libs/l/caf\\xe9.sch"
    diff = d["cells"][0]["views"][0]["diff"]                   # read from git's own bytes of the name
    assert diff["kind"] == "schematic" and "error" not in diff and {c["do"] for c in diff["changes"]} == {"add"}

    # added in a commit, then modified: the diffs of both, and the escaped name given back finds the file
    cafe.write_text(BUF)
    added = commit(api.d, "cafe")
    [view] = [v for c in get(api, f"/api/p/p/git/diff?rev={added}")["cells"] for v in c["views"]]
    assert view["status"] == "added" and "error" not in view["diff"]
    assert {c["id"] for c in view["diff"]["changes"]} >= {"inst:x1", "inst:x2"}
    cafe.write_text(BUF.replace("200 0 0 0 {name=x2}", "400 0 0 0 {name=x2}"))
    st = get(api, "/api/p/p/git")
    [c] = st["cells"]
    assert (c["lib"], c["cell"], c["views"][0]["status"]) == ("l", "caf\\xe9", "modified")
    one = get(api, "/api/p/p/git/diff?rev=worktree&lib=l&cell=caf%5Cxe9")       # the name as status gave it
    [view] = [v for c in one["cells"] for v in c["views"]]
    assert view["path"] == "libs/l/caf\\xe9.sch" and [x["id"] for x in view["diff"]["changes"]] == [
        "inst:x2:placement"]
    assert [x["sha"] for x in get(api, "/api/p/p/git/log?lib=l&cell=caf%5Cxe9")["commits"]] == [added]
    assert get(api, "/api/p/p/git/log?lib=l&cell=caf%C3%A9")["commits"] == []    # the cell of that UTF-8 name: none


def test_project_in_a_subdirectory_of_its_repository(api):
    other = api.srv.store.create("q")
    write(other.path, "libs/q/x.sch", HEADER)
    git(api.root, "init", "-q")
    st = get(api, "/api/p/p/git")
    assert st["repo"] is True and st["prefix"] == "p/" and st["setup"] == {"merge": False, "diff": False,
                                                                           "hook": False, "gitignore": GITIGNORE}
    assert ("l", "inv", "schematic") in views(st) and "project.json" in others(st)
    assert all(not o["path"].startswith(("q/", "../")) for o in st["other"])
    first = commit(api.root, "both projects")
    write(other.path, "libs/q/x.sch", HEADER + "T {q} 0 0 0 0 0.4 0.4 {}\n")
    commit(api.root, "q only")
    write(api.d, "libs/l/inv.sch", edit(INV, "W=2", "W=4"))
    second = commit(api.root, "p")
    log = get(api, "/api/p/p/git/log")
    assert [c["sha"] for c in log["commits"]] == [second, first] and log["more"] is False
    assert views(log["commits"][0]) == {("l", "inv", "schematic"): "modified"}
    d = get(api, f"/api/p/p/git/diff?rev={second}")
    assert [(c["lib"], c["cell"]) for c in d["cells"]] == [("l", "inv")]
    assert d["cells"][0]["views"][0]["path"] == "libs/l/inv.sch"
    setup(api, init=False)                                      # in the repository it is in: no git init here
    assert not (api.d / ".git").exists() and (api.d / ".gitattributes").is_file()
    assert get(api, "/api/p/p/git")["setup"] == {"merge": True, "diff": True, "hook": True, "gitignore": []}


def test_undine_output_is_never_listed(api, monkeypatch):
    """Job results, tapeout bundles, the assistant's log, the installed dependencies and a legacy layout.oas a split
    left are not the design: never listed, even in a repository git-setup's .gitignore is not in (a plain git init),
    nor where a commit has them. A library's directory of such a name is listed all the same."""
    git(api.d, "init", "-q")
    for rel in ("runs/sim-1/out.raw", "runs/lvs-2/report.txt", "tapeout/gds/top.gds", "llm/chats/c1.json",
                ".undine/deps/lib/x.sch", "libs/l/layout.oas.pre-split"):
        write(api.d, rel, "x\n")
    write(api.d, "libs/l/runs/notes.txt", "kept\n")             # in a library: not a job's
    st = get(api, "/api/p/p/git")
    assert "libs/l/runs/notes.txt" in others(st)
    assert not [o for o in others(st) if o.startswith(("runs/", "tapeout/", "llm/", ".undine/"))
                or o.endswith(".pre-split")]
    git(api.d, "add", "-A")                                      # committed all the same
    git(api.d, "commit", "-qm", "everything")
    write(api.d, "runs/sim-1/out.raw", "y\n")
    git(api.d, "commit", "-qam", "a job's output only")
    log = get(api, "/api/p/p/git/log")
    assert [c["subject"] for c in log["commits"]] == ["everything"]     # the job-only commit changed nothing listed
    first = log["commits"][0]
    d = get(api, f"/api/p/p/git/diff?rev={first['sha']}")
    assert first["other"] == len(d["other"]) and "libs/l/runs/notes.txt" in others(d)
    assert not [o for o in others(d) if o.startswith(("runs/", "tapeout/", "llm/", ".undine/"))]
    write(api.d, "runs/sim-1/out.raw", "z\n")                  # tracked and modified: still not listed
    assert not [o for o in others(get(api, "/api/p/p/git")) if o.startswith("runs/")]
    write(api.d, "libs/l/inv.sch", edit(INV, "W=2", "W=4"))
    git(api.d, "commit", "-qam", "inv, and a job's output")
    [c, _] = get(api, "/api/p/p/git/log?lib=l&cell=inv")["commits"]    # all the commit changed: but the job's
    assert c["subject"] == "inv, and a job's output" and c["other"] == 0

    monkeypatch.setattr(history, "MAX_OTHER", 3)               # many other files: the first ones by path
    for i in range(5):
        write(api.d, f"notes/n{i}.txt", "x\n")
    assert [o["path"] for o in get(api, "/api/p/p/git")["other"]] == [f"notes/n{i}.txt" for i in range(3)]
    assert len(get(api, "/api/p/p/git/diff?rev=worktree")["other"]) == 3


def test_a_conflict_in_project_json_leaves_the_history_readable(api):
    """project.json has no merge driver: both branches adding a library or editing its description leave it with
    conflict markers. The project's git routes still answer (HEAD's libraries), so the conflict can be seen."""
    setup(api)
    commit(api.d, "base")
    main = git(api.d, "symbolic-ref", "--short", "HEAD")

    def describe(v: str) -> None:
        prj = json.loads((api.d / "project.json").read_text())
        prj["description"] = v
        write(api.d, "project.json", json.dumps(prj, indent=2) + "\n")
    git(api.d, "checkout", "-qb", "side")
    describe("side")
    commit(api.d, "side")
    git(api.d, "checkout", "-q", main)
    describe("main")
    write(api.d, "libs/l/inv.sch", edit(INV, "W=2", "W=4"))
    commit(api.d, "main")
    git(api.d, "merge", "-q", "--no-edit", "side", ok=False)
    assert "<<<<<<<" in (api.d / "project.json").read_text()
    st = get(api, "/api/p/p/git")
    assert st["operation"] == "merge" and st["conflicted"] == ["project.json"]
    assert others(st) == {"project.json": "conflicted"}
    assert [c["subject"] for c in get(api, "/api/p/p/git/log")["commits"]] == ["main", "base"]
    assert views(get(api, "/api/p/p/git/log?limit=1")["commits"][0]) == {("l", "inv", "schematic"): "modified"}
    assert others(get(api, "/api/p/p/git/diff?rev=worktree")) == {"project.json": "conflicted"}
    assert "resolve it first" in get(api, "/api/p/p/git/verify", 400)["detail"]   # the merged libraries: unknown


# ---------------------------------------------------------------- log


def test_log_by_cell_with_pages(api):
    setup(api)
    c1 = commit(api.d, "base\n\nthe inverter and its buffer")
    write(api.d, "libs/l/inv.sch", edit(INV, "W=2", "W=3"))
    write(api.d, "libs/l/buf.sch", BUF + "T {buf} 0 0 0 0 0.4 0.4 {}\n")
    c2 = commit(api.d, "inv and buf")
    write(api.d, "libs/l/buf.sch", BUF)
    c3 = commit(api.d, "buf")
    git(api.d, "tag", "v1")
    layout(api.d / "libs/l", (1000, 0, 1500, 500))
    write(api.d, "history/l.jsonl", "{}\n")
    c4 = commit(api.d, "inv layout")

    log = get(api, "/api/p/p/git/log")
    assert [c["sha"] for c in log["commits"]] == [c4, c3, c2, c1] and log["more"] is False
    top = log["commits"][0]
    assert set(top) == {"sha", "short", "parents", "subject", "body", "author", "email", "date", "refs", "cells",
                        "other"}
    assert top["parents"] == [c3] and top["author"] == "t" and top["email"] == "t@example.com"
    assert top["short"] and c4.startswith(top["short"]) and top["date"][:4].isdigit() and "T" in top["date"]
    assert views(top) == {("l", "inv", "layout"): "modified"} and top["other"] == 1
    assert log["commits"][1]["refs"] == ["v1"]
    assert log["commits"][3]["body"] == "the inverter and its buffer" and log["commits"][3]["subject"] == "base"
    assert log["commits"][3]["other"] >= 3                     # project.json, undine.lock, .gitattributes ...

    page = get(api, "/api/p/p/git/log?limit=2")
    assert [c["sha"] for c in page["commits"]] == [c4, c3] and page["more"] is True
    page = get(api, "/api/p/p/git/log?limit=2&skip=2")
    assert [c["sha"] for c in page["commits"]] == [c2, c1] and page["more"] is False

    # one cell: the commits that touched its files, each with everything it changed
    inv = get(api, "/api/p/p/git/log?lib=l&cell=inv")
    assert [c["sha"] for c in inv["commits"]] == [c4, c2, c1]
    assert views(inv["commits"][1]) == {("l", "inv", "schematic"): "modified", ("l", "buf", "schematic"): "modified"}
    assert inv["commits"][0]["other"] == 1
    assert [c["sha"] for c in get(api, "/api/p/p/git/log?lib=l&cell=buf")["commits"]] == [c3, c2, c1]
    assert [c["sha"] for c in get(api, "/api/p/p/git/log?lib=l&cell=via1")["commits"]] == [c1]
    assert get(api, "/api/p/p/git/log?lib=l&cell=nothing") == {"commits": [], "more": False}
    assert get(api, "/api/p/p/git/log?lib=l", 400)["detail"] == "lib and cell go together"
    assert api.client.get("/api/p/p/git/log?lib=l&cell=..").status_code == 400
    assert api.client.get("/api/p/p/git/log?limit=0").status_code == 400
    assert api.client.get("/api/p/nope/git/log").status_code == 404


# ---------------------------------------------------------------- diff


def test_diff_of_a_commit_and_of_the_working_tree(api):
    setup(api)
    c1 = commit(api.d, "base")
    write(api.d, "libs/l/inv.sch", edit(INV, "W=2", "W=4"))
    layout(api.d / "libs/l", (1000, 0, 1500, 500))
    write(api.d, "libs/l/inv.sv", "module inv; endmodule\n")
    write(api.d, "history/l.jsonl", "{}\n")
    c2 = commit(api.d, "wider, and a box")

    d = get(api, f"/api/p/p/git/diff?rev={c2}")
    assert d["base"] == c1 and d["rev"] == c2
    assert others(d) == {"history/l.jsonl": "added"}
    [inv] = d["cells"]
    assert (inv["lib"], inv["cell"]) == ("l", "inv")
    by = {v["view"]: v for v in inv["views"]}
    assert set(by) == {"schematic", "layout", "rtl"} and set(by["schematic"]) == {"view", "path", "status", "diff"}
    assert by["schematic"]["status"] == "modified" and by["rtl"]["status"] == "added"
    w = [c for c in by["schematic"]["diff"]["changes"] if c["id"] == "inst:M1:prop:W"]
    assert w and w[0]["do"] == "mod" and (w[0]["before"], w[0]["after"]) == ("2", "4")
    lay = by["layout"]["diff"]
    assert lay["kind"] == "layout" and any(c["do"] in ("add", "mod") and "68/20" in c["what"]
                                           for c in lay["changes"])
    assert lay["geometry"] and lay["geometry"][0]["layer"] == "68/20" and lay["geometry"][0]["xor_um2"] == 0.25
    assert by["rtl"]["diff"] == {"kind": "text", "do": "add"}

    root = get(api, f"/api/p/p/git/diff?rev={c1}")              # a root commit: against the empty tree
    assert root["base"] is None and root["rev"] == c1
    assert set(views(root).values()) == {"added"} and ("l", "via1", "layout") in views(root)

    # the working tree against HEAD, one cell
    write(api.d, "libs/l/inv.sch", edit(edit(INV, "W=2", "W=4"), "L=0.15\nW=1", "L=0.3\nW=1"))
    write(api.d, "libs/l/buf.sch", BUF + "T {buf} 0 0 0 0 0.4 0.4 {}\n")
    wt = get(api, "/api/p/p/git/diff?rev=worktree&lib=l&cell=inv")
    assert wt["base"] == c2 and wt["rev"] is None and wt["other"] == []
    [sch] = [v for c in wt["cells"] for v in c["views"]]
    assert sch["path"] == "libs/l/inv.sch" and sch["status"] == "modified"
    assert [c["id"] for c in sch["diff"]["changes"]] == ["inst:M2:prop:L"]
    assert {c["cell"] for c in get(api, "/api/p/p/git/diff?rev=worktree")["cells"]} == {"inv", "buf"}
    assert get(api, "/api/p/p/git/diff?rev=nosuchrev", 400)["detail"]
    assert api.client.get("/api/p/p/git/diff").status_code == 422    # rev is required


# ---------------------------------------------------------------- merges


def test_verify_a_merge_commit_and_a_merge_in_progress(api):
    setup(api)
    commit(api.d, "base")
    main = git(api.d, "symbolic-ref", "--short", "HEAD")
    git(api.d, "checkout", "-qb", "side")
    write(api.d, "libs/l/inv.sch", edit(INV, "L=0.15\nW=2", "L=0.15\nW=3"))     # M1
    side = commit(api.d, "M1 wider")
    git(api.d, "checkout", "-q", main)
    write(api.d, "libs/l/inv.sch", edit(INV, "L=0.15\nW=1", "L=0.15\nW=5"))     # M2
    ours = commit(api.d, "M2 wider")
    git(api.d, "merge", "-q", "--no-edit", "side")
    merged = git(api.d, "rev-parse", "HEAD")
    assert get(api, "/api/p/p/git/log?limit=1")["commits"][0]["parents"] == [ours, side]

    v = get(api, f"/api/p/p/git/verify?rev={merged}")
    assert v["commit"] == merged and v["parents"] == [ours, side]
    assert {(x["lib"], x["cell"]) for x in v["verify"]} == {("l", "inv"), ("l", "buf")}
    reasons = {x["cell"]: x["reason"] for x in v["verify"]}
    assert reasons == {"inv": {"kind": "merged"},
                       "buf": {"kind": "side", "side": "ours", "below": [{"lib": "l", "cell": "inv", "side": None}]}}
    assert v["notes"] == [] and v["note_codes"] == []
    assert v["merged"] == ["libs/l/inv.sch"] and v["no_report"] == []
    assert get(api, f"/api/p/p/git/verify?rev={ours}", 400)["detail"].endswith("is not a merge commit")
    d = get(api, f"/api/p/p/git/diff?rev={merged}")            # against the first parent: theirs' change
    assert [c["id"] for v in d["cells"][0]["views"] for c in v["diff"]["changes"]] == ["inst:M1:prop:W"]

    # both sides change the same property: the merge stops on a conflict
    git(api.d, "checkout", "-qb", "other", "side")
    write(api.d, "libs/l/inv.sch", edit(INV, "L=0.15\nW=2", "L=0.15\nW=7"))
    commit(api.d, "M1 at 7")
    git(api.d, "checkout", "-q", main)
    write(api.d, "libs/l/inv.sch", edit((api.d / "libs/l/inv.sch").read_text(), "W=3", "W=9"))
    commit(api.d, "M1 at 9")
    git(api.d, "merge", "-q", "--no-edit", "other", ok=False)
    st = get(api, "/api/p/p/git")
    assert st["operation"] == "merge" and st["conflicted"] == ["libs/l/inv.sch"]
    assert views(st) == {("l", "inv", "schematic"): "conflicted"}
    now = get(api, "/api/p/p/git/verify")                       # the merge in progress: its open conflicts
    assert now["operation"] == "merge" and now["commit"] is None
    [u] = now["unmerged"]
    assert u["path"] == "libs/l/inv.sch" and [(c["id"], c["ours"], c["theirs"]) for c in u["conflicts"]] == [
        ("inst:M1:prop:W", "9", "7")]
    git(api.d, "merge", "--abort")
    assert "no merge in progress" in get(api, "/api/p/p/git/verify", 400)["detail"]
    assert get(api, "/api/p/p/git")["operation"] is None


def test_verify_with_names_that_are_not_utf8(api):
    """merge-check lists the whole repository's files: one name that is not UTF-8 anywhere must not make every check
    fail, and a conflicted file of such a name is shown escaped, as status shows it."""
    setup(api)
    cafe = os.fsdecode(b"libs/l/caf\xe9.sch")
    write(api.d, cafe, BUF)
    commit(api.d, "base")
    main = git(api.d, "symbolic-ref", "--short", "HEAD")
    git(api.d, "checkout", "-qb", "side")
    write(api.d, "libs/l/inv.sch", edit(INV, "L=0.15\nW=2", "L=0.15\nW=3"))
    write(api.d, cafe, BUF.replace("{name=x2}", "{name=x3}"))
    side = commit(api.d, "side")
    git(api.d, "checkout", "-q", main)
    write(api.d, "libs/l/inv.sch", edit(INV, "L=0.15\nW=1", "L=0.15\nW=5"))
    write(api.d, cafe, BUF.replace("200 0 0 0", "300 0 0 0"))
    commit(api.d, "main")
    git(api.d, "merge", "-q", "--no-edit", "side")
    v = get(api, "/api/p/p/git/verify?rev=HEAD")
    assert v["merged"] == ["libs/l/caf\\xe9.sch", "libs/l/inv.sch"] and v["no_report"] == []
    assert {(x["lib"], x["cell"]) for x in v["verify"]} == {("l", "caf\\xe9"), ("l", "inv"), ("l", "buf")}

    git(api.d, "checkout", "-qb", "other", side)              # both sides rename x2 differently: a conflict
    write(api.d, cafe, BUF.replace("{name=x2}", "{name=x7}"))
    commit(api.d, "other")
    git(api.d, "checkout", "-q", main)
    write(api.d, cafe, (api.d / cafe).read_text().replace("{name=x3}", "{name=x9}"))
    commit(api.d, "main again")
    git(api.d, "merge", "-q", "--no-edit", "other", ok=False)
    assert get(api, "/api/p/p/git")["conflicted"] == ["libs/l/caf\\xe9.sch"]
    now = get(api, "/api/p/p/git/verify")
    assert [u["path"] for u in now["unmerged"]] == ["libs/l/caf\\xe9.sch"] and now["unmerged"][0]["conflicts"]


# ---------------------------------------------------------------- previews and open layouts


def test_changeset_preview_has_no_history(api):
    setup(api)
    commit(api.d, "base")
    meta = Changesets(api.srv).create("p", "try")
    preview = meta["preview"]
    st = get(api, f"/api/p/{preview}/git")
    assert st["repo"] is False and st["preview"] is True and st["cells"] == [] and st["setup"] is None
    # a worktree of its branch (docs/design/changeset-branches.md): the status names the changeset, branch and base
    assert (st["changeset"], st["branch"], st["base"]) == (meta["id"], f"undine/p/{meta['id']}", meta["git"]["base"])
    for url in ("git/log", "git/diff?rev=worktree", "git/verify?rev=HEAD"):
        assert "changeset preview" in get(api, f"/api/p/{preview}/{url}", 400)["detail"]
    assert api.client.post(f"/api/p/{preview}/git/setup").status_code == 400
    assert api.client.get("/api/p/p~cs-nope/git").status_code == 404
    assert api.client.get("/api/p/nope/git").status_code == 404


def test_unsaved_layout_edits_are_listed(api):
    setup(api)
    commit(api.d, "base")
    assert get(api, "/api/p/p/git")["unsaved"] == []
    db = api.srv.db("p", "l")
    assert get(api, "/api/p/p/git")["unsaved"] == []           # open, not edited
    db.new_cell("nand")
    db.create("inv", "box", 0, [3000, 0, 3500, 500])
    assert get(api, "/api/p/p/git")["unsaved"] == [{"lib": "l", "cells": ["inv", "nand"]}]
    db.save()
    st = get(api, "/api/p/p/git")
    assert st["unsaved"] == [] and views(st)[("l", "nand", "layout")] == "untracked"
    assert views(st)[("l", "inv", "layout")] == "modified"
    assert history.status(api.d)["unsaved"] == []              # the server's to fill in


def test_the_unsaved_check_hashes_outside_the_layout_lock(api, monkeypatch):
    """Hashing every shape of a big library takes seconds: the status poll hashes a copy, and edits and tiles of the
    layout go on meanwhile."""
    setup(api)
    commit(api.d, "base")
    db = api.srv.db("p", "l")
    db.create("inv", "box", 0, [3000, 0, 3500, 500])
    real, held = layoutstore.snapshot, []

    def snapshot(ly):
        got = []

        def try_lock() -> None:                                # as an edit in another request would
            got.append(db.lock.acquire(timeout=5))
            if got[0]:
                db.lock.release()
        t = threading.Thread(target=try_lock)
        t.start()
        t.join()
        held.append(not got[0])
        return real(ly)
    monkeypatch.setattr(layoutstore, "snapshot", snapshot)
    assert get(api, "/api/p/p/git")["unsaved"] == [{"lib": "l", "cells": ["inv"]}]
    assert held == [False]
    assert get(api, "/api/p/p/git")["unsaved"] == [{"lib": "l", "cells": ["inv"]}]
    assert held == [False]                                     # the same version: not hashed again
    db.undo("inv")
    assert get(api, "/api/p/p/git")["unsaved"] == [{"lib": "l", "cells": []}]     # cancelled out, the log has it


def test_stale_layouts_are_listed(api):
    """An open layout with unsaved edits whose cell files changed on disk (a checkout, a merge) cannot be saved until
    the edits are dropped or reloaded: the status says which (LayoutDB.disk_conflict, disk_cells)."""
    assert get(api, "/api/p/p/git")["stale"] == []
    api.srv.db("p", "l")                                       # open, its files as read
    api.srv.dbs[("p", "z")] = SimpleNamespace(dirty=False, disk_conflict=True, disk_cells=["top", "inv"])
    api.srv.dbs[("q", "z")] = SimpleNamespace(dirty=False, disk_conflict=True, disk_cells=["x"])   # another project
    assert get(api, "/api/p/p/git")["stale"] == [{"lib": "z", "cells": ["inv", "top"]}]
    setup(api)
    st = get(api, "/api/p/p/git")
    assert st["stale"] == [{"lib": "z", "cells": ["inv", "top"]}] and st["unsaved"] == []


def test_a_project_json_with_conflict_markers_is_listed_and_named(tmp_path):
    """git left conflict markers in project.json: the project list still answers (the project with its error), and
    opening it says why instead of failing with a 500."""
    from fastapi.testclient import TestClient
    from undine.server.api import Server, create_app
    srv = Server(tmp_path, {}, [tmp_path], llm_policy="none")
    srv.store.create("good")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "project.json").write_text('<<<<<<< ours\n{"name": "bad"}\n=======\n{"name": "bad"}\n>>>>>>> theirs\n')
    client = TestClient(create_app(srv, None))
    r = client.get("/api/projects")
    assert r.status_code == 200
    by = {p["name"]: p for p in r.json()}
    assert "error" not in by["good"] and "conflict markers" in by["bad"]["error"]
    r = client.get("/api/p/bad")
    assert r.status_code == 400 and "conflict markers" in r.json()["detail"]


def test_verify_while_a_rebase_is_stopped_at_a_break(tmp_path, git_env):
    """git rebase stopped at `break` (or a failed exec): no REBASE_HEAD, nothing being merged; verify answers an empty
    check of the rebase instead of an error."""
    from undine.vcs import gitmerge
    d = tmp_path / "r"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    for n in ("one", "two", "three"):
        write(d, f"{n}.txt", n)
        git(d, "add", "-A")
        git(d, "commit", "-qm", n)
    r = subprocess.run(["git", "rebase", "-x", "false", "HEAD~2"], cwd=d, capture_output=True, text=True)
    assert r.returncode != 0
    check = gitmerge.merge_check(d)
    assert check["operation"] == "rebase" and check["verify"] == [] and check["unmerged"] == []


def test_a_whole_text_saved_over_a_file_git_changed_is_refused(api):
    """The RTL editor and the intent's YAML view send back the whole text: loaded before a git merge, saving it would
    undo the merge without a word, so a save that names the text it was edited from is refused once the file moved on."""
    d = api.d
    write(d, "libs/l/cnt.sv", "module cnt(input clk);\nendmodule\n")
    r = get(api, "/api/p/p/l/l/c/cnt/rtl")
    base = r["sha"]
    write(d, "libs/l/cnt.sv", "module cnt(input clk, input rst);\nendmodule\n")     # git merged a port in
    mine = "module cnt(input clk);\n// mine\nendmodule\n"
    r = api.client.post("/api/p/p/l/l/c/cnt/rtl", json={"op": "save", "text": mine, "base": base})
    assert r.status_code == 409 and "changed since it was loaded" in r.json()["detail"]
    assert "rst" in (d / "libs/l/cnt.sv").read_text()
    fresh = get(api, "/api/p/p/l/l/c/cnt/rtl")["sha"]
    r = api.client.post("/api/p/p/l/l/c/cnt/rtl", json={"op": "save", "text": mine, "base": fresh})
    assert r.status_code == 200 and "// mine" in (d / "libs/l/cnt.sv").read_text()

    v = get(api, "/api/p/p/l/l/c/inv/intent")
    write(d, "libs/l/inv.intent.yaml", "version: 1\nnotes: merged in from another branch\n")
    r = api.client.post("/api/p/p/l/l/c/inv/intent/edit", json={"op": "replace", "text": "notes: mine\n", "base": v["sha"]})
    assert r.status_code == 409 and "merged in" in (d / "libs/l/inv.intent.yaml").read_text()
    v = get(api, "/api/p/p/l/l/c/inv/intent")
    r = api.client.post("/api/p/p/l/l/c/inv/intent/edit", json={"op": "replace", "text": "notes: mine\n", "base": v["sha"]})
    assert r.status_code == 200 and "mine" in (d / "libs/l/inv.intent.yaml").read_text()
