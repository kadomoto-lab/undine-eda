"""Changesets on git branches (docs/design/changeset-branches.md), with real git: opening (git-setup of a project in no
repository, refusal of a repository not set up for Undine, the base commit made from the files, the branch, the
worktrees, the baseline read-only), the changeset's records after the base, the commits of the working copy (lease
end, checks), discard (evidence with the commits, the kept ref), the diff of the base, the preview's git status, older
changesets (review and discard only) and a project in a subdirectory of its repository; apply and revert as merges
(the merge commit on a clean project, the merge left uncommitted otherwise, a 409 conflict, the project's log, its
unsaved layout edits saved first, a revert keeping what the changeset added)."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import klayout.db as kdb
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.agents import agent_tools as T  # noqa: E402
from undine.layouts import layout  # noqa: E402
from undine.schematics import xschem  # noqa: E402
from undine.server import live  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store import changeset as csmod  # noqa: E402
from undine.store.changeset import ChangesetError, Changesets  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
from undine.vcs import branches  # noqa: E402
import test_changeset as tc  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def git(d: Path, *a: str, ok: bool = True) -> str:
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=d, capture_output=True, text=True)
    assert not ok or r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def tree(d: Path, rev: str) -> list[str]:
    return git(d, "ls-tree", "-r", "--name-only", rev).splitlines()


def boxes(db, cell="top"):
    return sorted(str(s.box) for s in db.cell(cell).shapes(db.layout.layer(68, 20)).each())


def status(d: Path) -> list[str]:
    """The user's `git status`, but the changeset records (in the project, not ignored: theirs to commit or not)."""
    return [x for x in git(d, "status", "--porcelain").splitlines() if "changesets/" not in x]


def set_r1(srv, project, value, cid=None, actor="user"):
    ed = srv.editor(project, "l", "top", "schematic")
    with ed.recording("set_props", {"id": "R1"}, actor, "why", cid):
        ed.set_props("i", "R1", {"value": value})


# ---------------------------------------------------------------- opening


def test_opening_makes_a_branch_from_the_files_and_its_worktrees():
    root, srv, cs = tc.setup()
    p = root / "p"
    set_r1(srv, "p", "2k")                                               # the project's history: seq 1
    srv.oplog("p", "l").append({"op": "note", "actor": "agent:x", "cell": None, "view": None, "effects": [],
                                "gseq": 7, "changeset": "cs-000000"})   # a record of a changeset applied before
    db = srv.db("p", "l")
    with db.recording("create", {}, "user"):
        db._commit("top", [("ins", db.layout.layer(68, 20), kdb.Box(0, 0, 50, 50))])     # unsaved: a journal
    (p / "runs" / "j1").mkdir(parents=True)
    (p / "runs" / "j1" / "out.txt").write_text("own output")
    (p / "notes.txt").write_text("untracked, not ignored")
    meta = cs.create("p", "Raise R1", "less current", "agent:t")
    cid, P, B = meta["id"], meta["preview"], meta["baseline"]
    g = meta["git"]
    # the project was in no repository: git-setup made it one, and its first commit (by the opener, no parent) is the
    # project's files as they were, the base's tree: the base's parent, the user's index and working tree its own
    first = meta["first_commit"]
    assert meta["git_setup"] is True and (p / ".git").is_dir() and (p / ".gitattributes").is_file()
    assert git(p, "rev-parse", "HEAD") == first and git(p, "rev-list", "--parents", "-n1", "HEAD") == first
    assert git(p, "log", "-1", "--format=%an|%cn|%s", first) == \
        "agent:t|Undine|Project p: its files when Undine set up its repository"
    assert tree(p, first) == tree(p, g["base"]) and git(p, "rev-parse", f"{g['base']}^") == first
    assert status(p) == [] and git(p, "diff", "--cached", "--name-only") == ""
    assert g["branch"] == f"undine/p/{cid}" and g["head"] == first and g["prefix"] == "" and g["tip"] is None
    assert git(p, "rev-parse", f"refs/heads/{g['branch']}") == g["base"] and g["commits"] == 0 and g["ref"] is None
    # the base is the files as they are, without Undine's own output and the changeset records
    files = tree(p, g["base"])
    assert {"libs/l/top.sch", "libs/l/r.sym", "libs/l/top.oas", "history/l.jsonl", "project.json", "notes.txt",
            ".gitignore"} <= set(files)
    assert not [f for f in files if f.startswith(("runs/", "changesets/"))]
    assert meta["libs"] == ["l"] and g["base_seq"] == {"l": 3}
    rec = json.loads((p / "changesets" / f"{cid}.json").read_text())
    assert rec["git"]["base_seq"] == {"l": 3} and set(rec["base"]) == {"l"}
    # the worktrees: the working copy on the branch, the baseline detached at the base
    shadow = root / ".changesets" / "p" / cid
    assert srv.project(P).path == shadow / "work" and srv.project(B).path == shadow / "base"
    assert git(shadow / "work", "symbolic-ref", "HEAD") == f"refs/heads/{g['branch']}"
    assert git(shadow / "base", "rev-parse", "HEAD") == g["base"]
    # the working copy starts from the files: the project's unsaved layout edit is not replayed there
    assert boxes(srv.db(P, "l")) == [] and not srv.db(P, "l").dirty and boxes(srv.db("p", "l")) == ["(0,0;50,50)"]
    assert tc.value(srv, P) == "2k"
    # its log is the changeset's own: numbering starts at 1, each record says whose it is, undo stops at the base
    assert srv.oplog(P, "l").read() == []
    srv.editor(P, "l", "top", "schematic").set_props("i", "R1", {"value": "3k"})        # no changeset named
    rec = srv.oplog(P, "l").read()[-1]
    assert rec["gseq"] == 1 and rec["changeset"] == cid and rec["seq"] == 4
    assert srv.editor(P, "l", "top", "schematic").undo() and tc.value(srv, P) == "2k"
    assert not srv.editor(P, "l", "top", "schematic").undo() and tc.value(srv, P) == "2k"   # the project's edit stays
    assert srv.editor(P, "l", "top", "schematic").redo()
    assert [o["op"] for lc in cs.get("p", cid)["changes"] for o in lc["ops"]] == ["edit"]
    assert [o["first"] for o in cs.timeline("p", cid)["ops"]] == [1, 2, 3]
    # the baseline is read-only, whatever route tries
    assert all(lib.readonly for lib in srv.project(B).libraries().values())
    with pytest.raises(PermissionError):
        srv.db(B, "l").create("top", "box", srv.db(B, "l").layout.layer(68, 20), [0, 0, 10, 10])
    c = TestClient(create_app(srv, None))
    assert c.post(f"/api/p/{B}/libs", json={"name": "z"}).status_code == 403
    assert c.post(f"/api/p/{B}/l/l/schematics", json={"name": "new"}).status_code == 403
    assert c.delete(f"/api/p/{B}/flows/f").status_code == 403
    assert not (shadow / "base" / "libs" / "z").exists()
    # a second changeset finds the repository set up, and makes no commit on the user's branch
    again = cs.create("p", "Another")
    assert again["git_setup"] is False and again["first_commit"] is None and git(p, "rev-parse", "HEAD") == first
    shutil.rmtree(root)


def test_a_repository_not_set_up_for_undine_is_refused(tmp_path):
    root = tmp_path / "projects"
    srv = Server(root, {}, [tmp_path])
    srv.store.create("p")
    git(tmp_path, "init", "-q")                                           # e.g. a home directory kept in git
    cs = Changesets(srv)
    with pytest.raises(ChangesetError, match="not set up for Undine.*git-setup"):
        cs.create("p", "T")
    with pytest.raises(csmod.NotSetUp) as refused:
        cs.create("p", "T")
    assert refused.value.top == tmp_path and refused.value.project == "p"
    command = f"undine --root {root} git-setup p"                 # not the command's default store: --root
    assert refused.value.command == command
    # the API says which refusal it is, where the repository is and the project the command sets up: the screen
    # words it (and names its own button)
    r = TestClient(create_app(srv, None)).post("/api/p/p/changesets", json={"title": "T", "reason": ""})
    body = r.json()
    assert r.status_code == 400 and body["code"] == "not_set_up" and body["top"] == str(tmp_path)
    assert body["project"] == "p" and body["command"] == command
    assert body["params"] == {"top": str(tmp_path), "project": "p", "command": command}
    assert "git-setup p`" in body["detail"]
    assert not (root / ".changesets").exists() and git(tmp_path, "branch", "--list", "undine/*") == ""
    assert git(tmp_path, "config", "--get", "merge.undine.driver", ok=False) == ""


def test_the_api_and_the_agent_say_what_was_set_up_and_name_the_branch():
    root, srv, cs = tc.setup()
    c = TestClient(create_app(srv, None))
    r = c.post("/api/p/p/changesets", json={"title": "T", "reason": "r"}).json()
    assert r["git_setup"] is True and r["git"]["branch"] == f"undine/p/{r['id']}"
    assert r["first_commit"] == r["git"]["head"] == git(root / "p", "rev-parse", "HEAD")
    assert set(r["git"]) >= {"branch", "base", "head", "prefix", "tip", "ref", "commits"}
    listed = c.get("/api/p/p/changesets").json()
    assert listed[0]["git"]["branch"] == r["git"]["branch"] and listed[0]["git"]["tip"] is None
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    out = json.loads(T.call(ctx, "open_changeset", {"title": "U", "reason": "r"})[0])
    assert out["branch"] == f"undine/p/{out['id']}" and out["branch"] in out["note"] and "git_setup" not in out
    assert "first_commit" not in out
    shutil.rmtree(root)
    root, srv, cs = tc.setup()
    out = json.loads(T.call(T.ToolCtx(srv, "p", actor="agent:t"), "open_changeset", {"title": "U", "reason": "r"})[0])
    assert out["git_setup"] is True and "git init" in out["note"] and "first commit" in out["note"]
    assert out["first_commit"] == git(root / "p", "rev-parse", "HEAD")
    shutil.rmtree(root)


# ---------------------------------------------------------------- the records of the changeset


def test_a_restore_takes_back_only_its_own_changesets_records(tmp_path):
    """A log can hold the records of a changeset applied before, gseq and all (a merge kept them): a restore of
    another changeset to its checkpoint does not take those back (oplog._before)."""
    log = OpLog(tmp_path / "l.jsonl")
    fx = lambda v: [{"k": "i", "id": "R1", "do": "mod", "id_after": "R1", "changes": {"props.value": [None, v]}}]  # noqa: E731
    for gseq, cs_, v in ((1, "cs-aaaaaa", "1"), (2, "cs-aaaaaa", "2"), (1, "cs-bbbbbb", "3"), (2, "cs-bbbbbb", "4")):
        log.append({"op": "set_props", "cell": "top", "view": "schematic", "effects": fx(v), "gseq": gseq, "changeset": cs_})
    log.append({"op": "restore", "to": 1, "cell": None, "view": None, "effects": [], "gseq": 3, "changeset": "cs-bbbbbb"})
    from undine.store import oplog
    assert [r["seq"] for r in oplog.net_records(log)] == [1, 2, 3]       # cs-aaaaaa's second record stays
    assert [r["seq"] for r in log.stacks("top", "schematic")[0]] == [1, 2, 3]
    log.floor = 2                                                          # the project's history up to seq 2
    assert [r["seq"] for r in oplog.net_records(log)] == [3] and [r["seq"] for r in log.read()] == [3, 4, 5]


# ---------------------------------------------------------------- commits of the working copy


def test_the_working_copy_is_committed_when_the_lease_ends_and_before_the_checks():
    root, srv, cs = tc.setup()
    p = root / "p"
    c = TestClient(create_app(srv, None))
    ctx = T.ToolCtx(srv, "p", actor="agent:test", owner="mcp:s1", owner_kind="mcp")
    assert not T.call(ctx, "open_changeset", {"title": "Raise R1", "reason": "less current"})[1]
    cid = ctx.changeset
    P = f"p~{cid}"
    status, head = git(p, "status", "--porcelain"), git(p, "rev-parse", "HEAD")     # the first commit the open made
    args = {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "10k"}, "reason": "faster"}
    assert not T.call(ctx, "set_instance_params", args)[1]
    db = srv.db(P, "l")
    with live.acting("mcp:s1"), db.recording("add_box", {}, "agent:test", "pad", cid):
        db._commit("top", [("ins", db.layout.layer(68, 20), kdb.Box(0, 0, 100, 100))])
    assert db.dirty
    srv.lease_ended()
    assert c.get(f"/api/p/p/changesets/{cid}").json()["git"]["commits"] == 0             # still writing
    srv.leases.release_owner("mcp:s1")
    srv.lease_ended()                                                      # its end: the work is committed
    g = c.get(f"/api/p/p/changesets/{cid}").json()["git"]
    assert g["commits"] == 1 and g["tip"] == git(p, "rev-parse", f"refs/heads/{g['branch']}") != g["base"]
    assert not srv.db(P, "l").dirty                                        # its layout saved first, as the system
    assert srv.oplog(P, "l").read()[-1]["op"] == "save" and srv.oplog(P, "l").read()[-1]["actor"] == "system"
    msg = git(p, "log", "-1", "--format=%an <%ae>|%cn|%B", g["tip"])
    assert msg.startswith("agent:test <undine@localhost>|Undine|Raise R1\n")
    assert f"Changeset {cid} of p: less current" in msg and "Actor: agent:test" in msg
    assert "- set_props l/top (schematic): faster" in msg and "- add_box l/top (layout): pad" in msg
    assert 'value=10k' in git(p, "show", f"{g['tip']}:libs/l/top.sch")
    assert git(p, "diff", "--name-only", g["base"], g["tip"]).splitlines() == [
        "history/l.jsonl", "libs/l/top.netlist.yaml", "libs/l/top.oas", "libs/l/top.sch"]       # the netlist view too
    assert git(p, "status", "--porcelain") == status                      # the user's working tree and index as they were
    assert git(p, "rev-parse", "HEAD") == head                             # and their branch
    # nothing new: no commit; a person's edit is committed before the checks
    srv.lease_ended()
    cs.verify("p", cid)
    assert c.get(f"/api/p/p/changesets/{cid}").json()["git"]["commits"] == 1
    set_r1(srv, P, "12k", cid)
    cs.verify("p", cid)
    g = c.get(f"/api/p/p/changesets/{cid}").json()["git"]
    assert g["commits"] == 2 and "- set_props l/top (schematic): why" in git(p, "log", "-1", "--format=%B", g["tip"])
    assert git(p, "rev-list", "--count", f"{g['base']}..{g['tip']}") == "2"
    # not while someone else writes: their lease's end commits
    srv.leases.acquire(cid, "turn:t-x", "turn")
    with live.acting("turn:t-x"):
        set_r1(srv, P, "13k", cid)
    cs.verify("p", cid)
    assert c.get(f"/api/p/p/changesets/{cid}").json()["git"]["commits"] == 2
    srv.leases.release(cid, "turn:t-x")
    c.get("/api/p/p/live")                                                 # a screen asks who writes: the end is seen
    assert c.get(f"/api/p/p/changesets/{cid}").json()["git"]["commits"] == 3
    # an MCP session's lease that lapsed (120 s without a write) ends too
    srv.leases.acquire(cid, "mcp:s2", "mcp")
    with live.acting("mcp:s2"):
        set_r1(srv, P, "14k", cid)
    srv.leases.get(cid).expires = 0
    c.get("/api/p/p/live")
    assert c.get(f"/api/p/p/changesets/{cid}").json()["git"]["commits"] == 4
    # what the server keeps beside the worktree never enters a commit
    assert (root / ".changesets" / "p" / cid / "tip.json").is_file()
    assert not [f for f in tree(p, g["branch"]) if f.endswith(("tip.json", live.FLOOR))]
    shutil.rmtree(root)


def test_discard_archives_the_commits_and_keeps_the_branch():
    root, srv, cs = tc.setup()
    p = root / "p"
    meta = cs.create("p", "Try", "r", "agent:t")
    cid, P = meta["id"], meta["preview"]
    set_r1(srv, P, "5k", cid, "agent:t")
    cs.discard("p", cid)
    closed = cs.get("p", cid)
    g = closed["git"]
    assert closed["status"] == "discarded" and g["ref"] == f"refs/undine/changesets/p/{cid}" and g["commits"] == 1
    assert git(p, "rev-parse", g["ref"]) == g["tip"] != g["base"]
    assert git(p, "branch", "--list", "undine/*") == "" and len(git(p, "worktree", "list").splitlines()) == 1
    assert not (root / ".changesets" / "p" / cid).exists()
    assert "value=5k" in git(p, "show", f"{g['ref']}:libs/l/top.sch")
    manifest = json.loads((p / closed["evidence"]["path"] / "manifest.json").read_text())
    assert manifest["version"] == 2 and manifest["commits"] == {"base": g["base"], "tip": g["tip"]}
    assert not (p / closed["evidence"]["path"] / "work" / "history").exists()
    with pytest.raises(KeyError):
        srv.project(P)
    # a changeset discarded unchanged keeps its branch too, at the base
    m2 = cs.create("p", "Nothing")
    g2 = cs.discard("p", m2["id"])["git"]
    assert g2["tip"] is None and git(p, "rev-parse", g2["ref"]) == g2["base"]
    shutil.rmtree(root)


# ---------------------------------------------------------------- review: the diff of the base


def test_the_diff_of_the_base_shows_what_no_operation_recorded():
    root, srv, cs = tc.setup()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "Diff", "r")
    cid, P = meta["id"], meta["preview"]
    set_r1(srv, P, "7k", cid)
    assert c.post(f"/api/p/{P}/l/l/schematics", json={"name": "extra"}).status_code == 200    # no log record
    d = c.get(f"/api/p/p/changesets/{cid}/diff").json()
    views = {(x["lib"], x["cell"], v["view"]): v["status"] for x in d["cells"] for v in x["views"]}
    assert views[("l", "top", "schematic")] == "modified" and views[("l", "extra", "schematic")] == "added"
    assert d["base"] == meta["git"]["base"] and d["rev"] is None
    one = c.get(f"/api/p/p/changesets/{cid}/diff?lib=l&cell=top").json()
    assert [x["cell"] for x in one["cells"]] == ["top"]
    cs.discard("p", cid)
    after = c.get(f"/api/p/p/changesets/{cid}/diff").json()
    assert after["rev"] == cs.get("p", cid)["git"]["tip"]
    assert {(x["cell"], v["view"]) for x in after["cells"] for v in x["views"]} >= {("top", "schematic"), ("extra", "schematic")}
    shutil.rmtree(root)


def test_the_git_status_of_a_preview_names_its_branch():
    root, srv, cs = tc.setup()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "T")
    st = c.get(f"/api/p/{meta['preview']}/git").json()
    assert st["repo"] is False and st["preview"] is True and st["changeset"] == meta["id"]
    assert st["branch"] == meta["git"]["branch"] and st["base"] == meta["git"]["base"] and st["unsaved"] == []
    db = srv.db(meta["preview"], "l")
    with db.recording("create", {}, "user", None, meta["id"]):
        db._commit("top", [("ins", db.layout.layer(68, 20), kdb.Box(0, 0, 10, 10))])
    assert c.get(f"/api/p/{meta['preview']}/git").json()["unsaved"] == [{"lib": "l", "cells": ["top"]}]
    assert c.get(f"/api/p/{meta['baseline']}/git").json()["changeset"] == meta["id"]
    assert c.get("/api/p/p~cs-0000zz/git").status_code == 404
    shutil.rmtree(root)


# ---------------------------------------------------------------- older changesets


def _old_form(root, srv, cs, status="open"):
    """A changeset as it was opened before changesets were git branches: copies, no "git" in its record."""
    prj = srv.project("p")
    cid = "cs-01d0ff"
    for side in ("base", "work"):
        d = root / ".changesets" / "p" / cid / side
        shutil.copytree(prj.path / "libs", d / "libs")
        shutil.copy2(prj.path / "project.json", d / "project.json")
    meta = {"id": cid, "project": "p", "title": "Old", "reason": "", "actor": "user", "created": "2026-01-01T00:00:00+0000",
            "status": status, "preview": f"p~{cid}", "baseline": f"p~{cid}~base", "base": {"l": {}}, "verify": [],
            "applied": None}
    cs._save("p", meta)
    return cid


def test_older_changesets_can_be_reviewed_and_discarded_only():
    root, srv, cs = tc.setup()
    c = TestClient(create_app(srv, None))
    cid = _old_form(root, srv, cs)
    P = f"p~{cid}"
    set_r1(srv, P, "8k", cid)
    rec = srv.oplog(P, "l").read()[-1]
    assert rec["gseq"] == 1 and rec["seq"] == 1                            # a copy's log: its own from the start
    got = c.get(f"/api/p/p/changesets/{cid}").json()
    assert "git" not in got and [o["op"] for lc in got["changes"] for o in lc["ops"]] == ["set_props"]
    r = c.post(f"/api/p/p/changesets/{cid}/apply")
    assert r.status_code == 400 and "open the proposal again as a new changeset" in r.json()["detail"]
    assert c.get(f"/api/p/p/changesets/{cid}/diff").status_code == 400
    assert tc.value(srv) == "1k" and not (root / "p" / ".git").exists()     # nothing was set up for it either
    assert c.post(f"/api/p/p/changesets/{cid}/discard").status_code == 200
    manifest = json.loads((root / "p" / "changesets" / cid / "evidence" / "manifest.json").read_text())
    assert manifest["version"] == 1 and "commits" not in manifest and "work/history/l.jsonl" in manifest["files"]
    assert not (root / ".changesets" / "p" / cid).exists()
    applied = _old_form(root, srv, cs, "applied")
    r = c.post(f"/api/p/p/changesets/{applied}/revert")
    assert r.status_code == 400 and "cannot be reverted" in r.json()["detail"]
    shutil.rmtree(root)


# ---------------------------------------------------------------- a project in a subdirectory of its repository


def test_a_project_in_a_subdirectory_of_its_repository(monkeypatch):
    from undine.__main__ import git_setup
    root, srv, cs = tc.setup()
    srv.store.create("q")
    git(root, "init", "-q")                                               # the store's root kept in git, set up
    git_setup(root / "p", say=None)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "projects")
    head = git(root, "rev-parse", "HEAD")
    meta = cs.create("p", "Sub", "r", "agent:t")
    cid, P, g = meta["id"], meta["preview"], meta["git"]
    shadow = root / ".changesets" / "p" / cid
    assert meta["git_setup"] is False and g["prefix"] == "p" and g["head"] == head
    assert srv.project(P).path == shadow / "work" / "p" and srv.project(meta["baseline"]).path == shadow / "base" / "p"
    assert (shadow / "work" / "q" / "project.json").is_file()            # the whole repository is checked out
    assert status(root) == []                                              # the worktrees are not the user's files
    made = []
    real_mkdtemp = csmod.tempfile.mkdtemp
    set_r1(srv, P, "2k", cid)
    set_r1(srv, P, "3k", cid)
    with monkeypatch.context() as m:
        m.setattr(csmod.tempfile, "mkdtemp", lambda **kw: made.append(kw["dir"]) or real_mkdtemp(**kw))
        cs.restore("p", cid, 1, "user")
    assert made == [shadow] and tc.value(srv, P) == "2k"                   # the snapshot beside the worktree
    cs.verify("p", cid)
    tip = cs.get("p", cid)["git"]["tip"]
    assert git(root, "diff", "--name-only", g["base"], tip).splitlines() == [
        "p/history/l.jsonl", "p/libs/l/top.netlist.yaml", "p/libs/l/top.sch"]
    assert git(root, "rev-parse", "HEAD") == head and status(root) == []
    d = cs.diff("p", cid)
    assert [(x["lib"], x["cell"]) for x in d["cells"]] == [("l", "top")]
    cs.discard("p", cid)
    assert git(root, "rev-parse", f"refs/undine/changesets/p/{cid}") == tip and not shadow.exists()
    assert status(root) == []
    shutil.rmtree(root)


# ---------------------------------------------------------------- apply and revert: merges


def committed_project():
    """tc.setup's project in a repository set up for Undine, everything committed: the user's tree is clean."""
    from undine.__main__ import git_setup
    root, srv, cs = tc.setup()
    p = root / "p"
    git_setup(p, say=None)
    git(p, "add", "-A")
    git(p, "commit", "-qm", "the project")
    return root, srv, cs, p


def log(srv, project="p", lib="l"):
    return srv.oplog(project, lib).read(limit=1000)


def test_apply_to_a_clean_project_is_a_merge_commit():
    root, srv, cs, p = committed_project()
    head = git(p, "rev-parse", "HEAD")
    meta = cs.create("p", "Raise R1", "less current", "agent:t")
    cid, P = meta["id"], meta["preview"]
    set_r1(srv, P, "10k", cid, "agent:t")
    assert branches.is_clean(p) and tc.value(srv) == "1k"
    out = cs.apply("p", cid, "user")
    a, g = out["applied"], out["git"]
    assert out["status"] == "applied" and a["uncommitted"] is False and a["commit"] == git(p, "rev-parse", "HEAD")
    assert a["why"] is None
    # a merge: the user's HEAD and the changeset's tip (kept under refs/undine/ once the branch is gone)
    assert git(p, "rev-list", "--parents", "-n1", "HEAD").split() == [a["commit"], head, g["tip"]]
    assert git(p, "rev-parse", g["ref"]) == g["tip"] == a["tip"] and git(p, "branch", "--list", "undine/*") == ""
    msg = git(p, "log", "-1", "--format=%an|%cn|%B")
    assert msg.startswith(f"user|Undine|Apply {cid}: Raise R1\n\nless current\n") and "Applied by: user" in msg
    assert f"the branch undine/p/{cid} (kept as refs/undine/changesets/p/{cid})" in msg
    assert "Checks:\n- LVS l/top (proposal): not run\n- LVS l/top (baseline): not run" in msg
    assert set(a["paths"]) == {"history/l.jsonl", "libs/l/top.sch", "libs/l/top.netlist.yaml"}
    # the index and the working tree are the commit's: nothing is left to commit but the changeset's own records
    assert status(p) == [] and git(p, "diff", "--cached", "--name-only") == ""
    assert "value=10k" in git(p, "show", "HEAD:libs/l/top.sch") and tc.value(srv) == "10k"
    assert git(p, "diff", "--name-only", head, "HEAD").splitlines() == sorted(a["paths"])
    assert not (root / ".changesets" / "p" / cid).exists() and len(git(p, "worktree", "list").splitlines()) == 1
    shutil.rmtree(root)


def test_apply_with_other_uncommitted_edits_is_left_in_the_working_tree():
    root, srv, cs, p = committed_project()
    head = git(p, "rev-parse", "HEAD")
    meta = cs.create("p", "Raise R1")
    set_r1(srv, meta["preview"], "10k", meta["id"])
    (p / "notes.txt").write_text("the user's, not committed")
    git(p, "add", "notes.txt")                                            # staged: the user's next commit
    out = cs.apply("p", meta["id"])
    assert out["applied"]["commit"] is None and out["applied"]["uncommitted"] is True
    assert out["applied"]["why"] == "changes"                             # what the review words for the person
    assert git(p, "rev-parse", "HEAD") == head and tc.value(srv) == "10k"
    st = git(p, "status", "--porcelain", "--", "notes.txt", "libs/l/top.sch")
    assert st.splitlines() == ["M libs/l/top.sch", "A  notes.txt"]                 # the user's index as it was
    assert git(p, "diff", "--cached", "--name-only") == "notes.txt"
    shutil.rmtree(root)


def test_a_concurrent_edit_of_the_same_instance_is_a_409_conflict():
    root, srv, cs = tc.setup()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "Raise R1")
    cid = meta["id"]
    set_r1(srv, meta["preview"], "3k", cid)
    set_r1(srv, "p", "2k")                                                # the project's own edit since the base
    files = {f: f.read_bytes() for f in (root / "p").rglob("*") if f.is_file() and not {"changesets", ".git"} & set(f.parts)}
    r = c.post(f"/api/p/p/changesets/{cid}/apply")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "merge_conflict" and "conflict" in body["detail"] and "nothing was written" in body["detail"]
    top = next(f for f in body["files"] if f["path"] == "libs/l/top.sch")
    assert top["kind"] == "schematic" and all({"id", "what", "base", "ours", "theirs"} <= set(x) for x in top["conflicts"])
    assert any("R1" in x["id"] + x["what"] for x in top["conflicts"])
    assert not any(f["path"].startswith("history/") for f in body["files"])      # the log is no file to merge by lines
    # nothing written: the project's files and log as they were; the changeset still open, its branch still there
    assert {f: f.read_bytes() for f in files} == files and tc.value(srv) == "2k"
    assert cs.get("p", cid)["status"] == "open" and branches.ref_exists(root / "p", f"undine/p/{cid}")
    with pytest.raises(csmod.MergeConflict):
        cs.apply("p", cid)
    shutil.rmtree(root)


def test_a_preview_name_in_a_changeset_route_stands_for_its_project():
    # the routes take the project from the path, and a screen in the preview may send its name: the changeset is read
    # from, and merged into, the real project, never its own worktree
    root, srv, cs, p = committed_project()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "Raise R1")
    cid, P = meta["id"], meta["preview"]
    set_r1(srv, P, "10k", cid)
    assert [x["id"] for x in c.get(f"/api/p/{P}/changesets").json()] == [cid]
    assert c.get(f"/api/p/{P}/changesets/{cid}").json()["git"]["branch"] == f"undine/p/{cid}"
    r = c.post(f"/api/p/{P}/changesets/{cid}/apply")
    assert r.status_code == 200, r.text
    assert r.json()["applied"]["commit"] == git(p, "rev-parse", "HEAD") and tc.value(srv) == "10k"
    r = c.post(f"/api/p/{P}/changesets/{cid}/revert")
    assert r.status_code == 200, r.text
    assert tc.value(srv) == "1k" and r.json()["reverted"]["commit"] == git(p, "rev-parse", "HEAD")
    shutil.rmtree(root)


def test_edits_of_different_cells_are_both_kept_and_the_log_has_both():
    root, srv, cs = tc.setup()
    lib = srv.project("p").library("l")
    aux = xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n")
    aux.comps = [xschem.XComp("r.sym", 0, 0, 0, 0, {"name": "R2", "value": "1k"})]
    (lib.path / "aux.sch").write_text(xschem.dump(aux))
    set_r1(srv, "p", "2k")                                                # history before the base: seq 1
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid, P = meta["id"], meta["preview"]
    set_r1(srv, P, "10k", cid, "agent:t")
    set_r1(srv, P, "11k", cid, "agent:t")
    assert srv.editor(P, "l", "top", "schematic").undo()                  # the changeset's own undo: 10k
    ed = srv.editor("p", "l", "aux", "schematic")
    with ed.recording("set_props", {"id": "R2"}, "user", "the project's"):
        ed.set_props("i", "R2", {"value": "7k"})                         # the project's edit of another cell
    before = log(srv)
    cs.apply("p", cid, "user")
    assert tc.value(srv) == "10k" and srv.editor("p", "l", "aux", "schematic").instance("R2")["props"]["value"] == "7k"
    recs = log(srv)
    # the project's log as it was, then the changeset's records (tagged, numbered on, "merged_seq" its own number;
    # an undo still points at what it undid), then the merge record naming the changeset
    assert recs[:len(before)] == before
    mine = recs[len(before):-1]
    assert [r["op"] for r in mine] == ["set_props", "set_props", "undo"] and {r["changeset"] for r in mine} == {cid}
    assert [r["seq"] for r in mine] == [3, 4, 5] and [r["merged_seq"] for r in mine] == [2, 3, 4]
    assert mine[2]["of"] == 4 and [r["gseq"] for r in mine] == [1, 2, 3]
    assert recs[-1] | {"t": None} == {"v": 1, "seq": 6, "t": None, "op": "merge", "actor": "user", "cell": None,
                                      "view": None, "args": {"changeset": cid, "action": "apply"}, "changeset": cid,
                                      "effects": [], "reason": f"applied changeset {cid}: Raise R1"}
    # an applied changeset is taken back with Revert: undo stops at the merge record, for its cells and the others
    assert not srv.editor("p", "l", "top", "schematic").undo() and not srv.editor("p", "l", "aux", "schematic").undo()
    assert tc.value(srv) == "10k" and log(srv) == recs
    shutil.rmtree(root)


def test_unsaved_layout_edits_of_the_project_are_saved_then_merged():
    root, srv, cs = tc.setup()
    meta = cs.create("p", "Pad", "r", "agent:t")
    cid, P = meta["id"], meta["preview"]
    wdb = srv.db(P, "l")
    with wdb.recording("create", {}, "agent:t", "pad", cid):
        wdb._commit("top", [("ins", wdb.layout.layer(68, 20), kdb.Box(0, 0, 100, 100))])
    db = srv.db("p", "l")
    with db.recording("create", {}, "user", "far away"):
        db._commit("top", [("ins", db.layout.layer(68, 20), kdb.Box(10000, 10000, 10100, 10100))])
    assert db.dirty
    out = cs.apply("p", cid, "user")
    # the project's edit was saved (recorded, with the changeset) before the merge, which kept both
    both = ["(0,0;100,100)", "(10000,10000;10100,10100)"]
    assert not db.dirty and boxes(db) == both
    assert boxes(Server(root, {}, [root]).db("p", "l")) == both                       # the files, read anew
    recs = log(srv)
    save = next(r for r in recs if r["op"] == "save" and r["actor"] == "system" and not r.get("merged_seq"))
    assert save["changeset"] == cid and "saved before the apply" in save["reason"]
    assert recs.index(save) < min(i for i, r in enumerate(recs) if r.get("merged_seq")) and recs[-1]["op"] == "merge"
    assert "libs/l/top.oas" in out["applied"]["paths"]
    assert layout._journal(recs)[1] == []                                 # nothing left to replay
    shutil.rmtree(root)


def test_revert_is_the_merge_the_other_way_and_keeps_what_the_changeset_added():
    root, srv, cs, p = committed_project()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid, P = meta["id"], meta["preview"]
    set_r1(srv, P, "10k", cid, "agent:t")
    assert c.post(f"/api/p/{P}/l/l/schematics", json={"name": "extra"}).status_code == 200     # a file it adds
    cs.apply("p", cid, "user")
    applied = git(p, "rev-parse", "HEAD")
    n = len(log(srv))
    set_r1(srv, "p", "12k")                                               # a later edit of what it changed
    r = c.post(f"/api/p/p/changesets/{cid}/revert")
    assert r.status_code == 409 and r.json()["code"] == "merge_conflict"
    assert [f["path"] for f in r.json()["files"]][:1] == ["libs/l/top.sch"] and tc.value(srv) == "12k"
    assert cs.get("p", cid)["status"] == "applied"
    assert srv.editor("p", "l", "top", "schematic").undo() and tc.value(srv) == "10k"   # after the merge: undoable
    git(p, "checkout", "--", "history/l.jsonl")                           # the user's tree clean again
    assert branches.is_clean(p)
    out = c.post(f"/api/p/p/changesets/{cid}/revert", headers={"x-undine-actor": "user"}).json()
    rv = out["reverted"]
    assert out["status"] == "reverted" and tc.value(srv) == "1k"
    # kept: the cell it made, and the netlist of top it added (the base had none yet), which follows the schematic
    assert (p / "libs" / "l" / "extra.sch").is_file()
    assert rv["kept"] == ["libs/l/extra.netlist.yaml", "libs/l/extra.sch", "libs/l/top.netlist.yaml"]
    # a commit with the user's HEAD as its one parent; the tree is the base's but for what the changeset added
    assert rv["commit"] == git(p, "rev-parse", "HEAD") and git(p, "rev-list", "--parents", "-n1", "HEAD").split() == \
        [rv["commit"], applied]
    assert git(p, "log", "-1", "--format=%s") == f"Revert {cid}: Raise R1" and status(p) == []
    assert "value=1k" in git(p, "show", "HEAD:libs/l/top.sch")
    netlist = git(p, "show", "HEAD:libs/l/top.netlist.yaml")
    assert "1k" in netlist and "10k" not in netlist and "libs/l/top.netlist.yaml" in rv["paths"]
    # the log is not merged back: the records stay, then a merge record naming the revert
    recs = log(srv)
    assert len(recs) == n + 1 and recs[-1]["op"] == "merge" and recs[-1]["args"] == {"changeset": cid, "action": "revert"}
    assert not srv.editor("p", "l", "top", "schematic").undo() and tc.value(srv) == "1k"
    assert c.post(f"/api/p/p/changesets/{cid}/revert").status_code == 400                   # once
    shutil.rmtree(root)


def test_apply_and_revert_in_a_subdirectory_of_the_repository():
    from undine.__main__ import git_setup
    root, srv, cs = tc.setup()
    git(root, "init", "-q")
    git_setup(root / "p", say=None)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "projects")
    head = git(root, "rev-parse", "HEAD")
    set_r1(srv, "p", "2k")                                                # history before the base, uncommitted
    git(root, "add", "-A")
    git(root, "commit", "-qm", "R1 2k")
    meta = cs.create("p", "Sub", "r", "agent:t")
    cid, P = meta["id"], meta["preview"]
    set_r1(srv, P, "3k", cid, "agent:t")
    out = cs.apply("p", cid, "user")
    assert out["applied"]["commit"] == git(root, "rev-parse", "HEAD") != head and tc.value(srv) == "3k"
    assert sorted(out["applied"]["paths"]) == ["history/l.jsonl", "libs/l/top.netlist.yaml", "libs/l/top.sch"]
    assert git(root, "diff", "--name-only", "HEAD~1", "HEAD").splitlines() == [
        "p/history/l.jsonl", "p/libs/l/top.netlist.yaml", "p/libs/l/top.sch"]
    recs = log(srv)
    assert [r["op"] for r in recs] == ["set_props", "set_props", "merge"] and recs[1]["merged_seq"] == 2
    assert status(root) == []
    cs.revert("p", cid, "user")
    assert tc.value(srv) == "2k" and status(root) == [] and git(root, "log", "-1", "--format=%s").startswith("Revert")
    shutil.rmtree(root)
