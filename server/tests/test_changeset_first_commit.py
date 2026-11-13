"""The project's first commit (docs/design/changeset-branches.md, The repository): a project in no repository, made one
by the opening of a changeset, gets its files committed at once, so that what is applied there is committed (on the
branch git init left unborn every apply stayed uncommitted, the review saying the project had other changes); an
apply left uncommitted says why ("no_commit", "changes", "failed"); the History panel's routes, git/setup (the first
commit after its git init) and git/first-commit (for a repository with none: a person's, no agent tool)."""
import json
import shutil
import sys
from pathlib import Path

import klayout.db as kdb
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.__main__ import git_setup  # noqa: E402
from undine.agents import agent_tools as T  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store import changeset as csmod  # noqa: E402
from undine.vcs import branches  # noqa: E402
import test_changeset as tc  # noqa: E402
import test_changeset_git as tg  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

FIRST = "Project p: its files when Undine set up its repository"


def subjects(p: Path) -> list[str]:
    """The user's branch, newest first (first parents)."""
    return tg.git(p, "log", "--first-parent", "--format=%s").splitlines()


def test_the_applies_into_a_project_undine_made_a_repository_are_commits():
    """The case the person met: a project in no repository, five changesets applied in a row, every merge left
    uncommitted ("the project had other uncommitted changes": it had none) and History saying "No commit yet"."""
    root, srv, cs = tc.setup()
    p = root / "p"
    applied = []
    for n in range(3):
        meta = cs.create("p", f"R1 {n}", "r", "agent:t")
        if n == 0:                                                          # git init, then the first commit at once
            assert meta["git_setup"] is True and meta["first_commit"] == meta["git"]["head"]
            assert subjects(p) == [FIRST] and tg.status(p) == []
        else:
            assert meta["git_setup"] is False and meta["first_commit"] is None
        tg.set_r1(srv, meta["preview"], f"{10 + n}k", meta["id"], "agent:t")
        a = cs.apply("p", meta["id"], "user")["applied"]
        assert a["commit"] == tg.git(p, "rev-parse", "HEAD") and a["uncommitted"] is False and a["why"] is None
        applied.append(f"Apply {meta['id']}: R1 {n}")
    assert subjects(p) == [*reversed(applied), FIRST] and tg.status(p) == [] and tc.value(srv) == "12k"
    shutil.rmtree(root)


def test_an_apply_onto_a_branch_with_no_commit_says_so_and_history_makes_the_first_commit():
    """A repository made by hand (git init, git-setup: nothing committed) or by an Undine before this one: the apply
    is left uncommitted, "no_commit", and the review leads to History, whose first commit takes the merge with the
    rest of the project; from then on applies are commits."""
    root, srv, cs = tc.setup()
    p = root / "p"
    git_setup(p, say=None)
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    assert meta["git_setup"] is False and meta["first_commit"] is None and meta["git"]["head"] is None
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")
    a = cs.apply("p", cid, "user")["applied"]
    assert a["commit"] is None and a["uncommitted"] is True and a["why"] == "no_commit"
    assert cs.get("p", cid)["applied"]["why"] == "no_commit"               # the record says so too
    assert tg.git(p, "rev-parse", "-q", "--verify", "HEAD", ok=False) == ""
    c = TestClient(create_app(srv, None))
    r = c.post("/api/p/p/git/first-commit")
    body = r.json()
    assert r.status_code == 200, r.text
    head = tg.git(p, "rev-parse", "HEAD")
    assert body["commit"] == head and body["head"]["sha"] == head and body["cells"] == [] and body["repo"] is True
    assert body["index_note"] is None
    assert tg.git(p, "log", "-1", "--format=%an|%cn|%s") == "user|Undine|Project p: its first commit, its files as they were"
    assert "value=10k" in tg.git(p, "show", "HEAD:libs/l/top.sch") and tg.status(p) == []
    assert not [f for f in tg.tree(p, "HEAD") if f.startswith(("changesets/", "runs/"))]
    r = c.post("/api/p/p/git/first-commit")                                 # once only
    assert r.status_code == 409 and r.json()["code"] == "has_commits" and tg.git(p, "rev-parse", "HEAD") == head
    meta = cs.create("p", "Raise R1 again", "r", "agent:t")
    tg.set_r1(srv, meta["preview"], "12k", meta["id"], "agent:t")
    a = cs.apply("p", meta["id"], "user")["applied"]
    assert a["commit"] == tg.git(p, "rev-parse", "HEAD") and a["why"] is None
    shutil.rmtree(root)


def test_undine_output_the_user_staged_before_setting_up_is_untracked_by_the_first_commit():
    """git init and git add -A by hand, before git-setup wrote .gitignore: runs/ and changesets/ are staged. The
    first commit leaves them out and untracks them, so History shows nothing and the next apply is committed."""
    root, srv, cs = tc.setup()
    p = root / "p"
    (p / "runs" / "sim").mkdir(parents=True)
    (p / "runs" / "sim" / "out.raw").write_text("raw\n")
    tg.git(p, "init", "-q")
    tg.git(p, "add", "-A")
    git_setup(p, say=None)
    c = TestClient(create_app(srv, None))
    r = c.post("/api/p/p/git/first-commit")
    body = r.json()
    assert r.status_code == 200 and body["index_note"] is None and body["cells"] == [] and body["other"] == [], r.text
    assert tg.git(p, "diff", "--cached", "--name-only") == "" and tg.git(p, "ls-files", "runs", "changesets") == ""
    assert tg.status(p) == [] and (p / "runs" / "sim" / "out.raw").is_file()      # untracked (ignored), kept
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    tg.set_r1(srv, meta["preview"], "10k", meta["id"], "agent:t")
    a = cs.apply("p", meta["id"], "user")["applied"]
    assert a["commit"] == tg.git(p, "rev-parse", "HEAD") and a["why"] is None
    shutil.rmtree(root)


def test_the_first_commit_waits_for_unsaved_layout_edits_but_at_the_opening():
    """A layout edit not saved to the cell files is a journal in the log, which the pre-commit hook refuses in a
    commit: the History panel's first commit (and Set up's) refuses it too, naming the layouts, until they are saved;
    the opening of a changeset, which nobody is there to save for, commits the log as its base has it."""
    root, srv, cs = tc.setup()
    p = root / "p"
    db = srv.db("p", "l")
    with db.recording("create", {}, "user"):
        db._commit("top", [("ins", db.layout.layer(68, 20), kdb.Box(0, 0, 50, 50))])
    assert db.dirty
    c = TestClient(create_app(srv, None))
    r = c.post("/api/p/p/git/setup")                                        # git init: the first commit refused
    body = r.json()
    assert r.status_code == 200 and body["init"] is True and body["commit"] is None and body["head"] is None, r.text
    refused = body["commit_refused"]
    assert refused["code"] == "unsaved" and "library l has layout edits not saved" in refused["detail"]
    assert refused["logs"] == [{"path": "history/l.jsonl", "lib": "l", "cells": ["top"]}]
    assert body["unsaved"] == [{"lib": "l", "cells": ["top"]}]                 # what the panel's button waits for
    r = c.post("/api/p/p/git/first-commit")
    assert r.status_code == 409 and r.json()["code"] == "unsaved" and r.json()["logs"] == refused["logs"]
    assert tg.git(p, "rev-parse", "-q", "--verify", "HEAD", ok=False) == ""
    db.save()
    r = c.post("/api/p/p/git/first-commit")
    assert r.status_code == 200 and r.json()["commit"] == tg.git(p, "rev-parse", "HEAD"), r.text
    shutil.rmtree(root)
    root, srv, cs = tc.setup()                                              # the opening: committed as it is
    p = root / "p"
    db = srv.db("p", "l")
    with db.recording("create", {}, "user"):
        db._commit("top", [("ins", db.layout.layer(68, 20), kdb.Box(0, 0, 50, 50))])
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    assert meta["first_commit"] == tg.git(p, "rev-parse", "HEAD") and meta["first_commit_note"] is None
    assert '"view":"layout"' in tg.git(p, "show", "HEAD:history/l.jsonl").replace(" ", "")
    shutil.rmtree(root)


def test_the_first_commit_with_the_index_held_by_another_git_says_what_to_run(monkeypatch):
    root, srv, cs = tc.setup()
    p = root / "p"
    git_setup(p, say=None)
    monkeypatch.setattr(branches, "_TRIES", 2)
    (p / ".git" / "index.lock").touch()                                    # a git of the user's, or one that crashed
    r = TestClient(create_app(srv, None)).post("/api/p/p/git/first-commit")
    body = r.json()
    assert r.status_code == 200 and body["commit"] == tg.git(p, "rev-parse", "HEAD"), r.text
    assert "index.lock" in body["index_note"] and "run `git reset`" in body["index_note"]
    (p / ".git" / "index.lock").unlink()
    shutil.rmtree(root)
    root, srv, cs = tc.setup()                                              # the opening: the agent is told
    p = root / "p"
    real = branches.first_commit

    def held(d, *a, **k):
        (Path(d) / ".git" / "index.lock").touch()
        try:
            return real(d, *a, **k)
        finally:
            (Path(d) / ".git" / "index.lock").unlink()

    monkeypatch.setattr(branches, "first_commit", held)
    out = json.loads(T.call(T.ToolCtx(srv, "p", actor="agent:t"), "open_changeset", {"title": "U", "reason": "r"})[0])
    assert out["first_commit"] == tg.git(p, "rev-parse", "HEAD") and "run `git reset`" in out["note"]
    assert out["note"].endswith("Tell the user")
    shutil.rmtree(root)


def test_a_commit_that_fails_leaves_the_merge_uncommitted_saying_so(monkeypatch):
    root, srv, cs, p = tg.committed_project()
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    tg.set_r1(srv, meta["preview"], "10k", meta["id"], "agent:t")

    def fails(*a, **k):
        raise RuntimeError("git commit-tree: fatal: cannot write the object")

    monkeypatch.setattr(csmod.branches, "commit_merge", fails)
    a = cs.apply("p", meta["id"], "user")["applied"]
    assert a["commit"] is None and a["uncommitted"] is True and a["why"] == "failed" and tc.value(srv) == "10k"
    tg.git(p, "add", "-A")                                                  # the person commits it
    tg.git(p, "commit", "-qm", "the merge")
    r = cs.revert("p", meta["id"], "user")["reverted"]                     # a revert's says so the same way
    assert r["commit"] is None and r["uncommitted"] is True and r["why"] == "failed" and tc.value(srv) == "1k"
    shutil.rmtree(root)


def test_a_refused_open_leaves_no_repository_nor_first_commit():
    """A library the worktrees cannot hold that only the tree shows (a link to what git does not keep): the first
    commit refuses it, and the repository made for the open goes with it."""
    root, srv, cs = tc.setup()
    p = root / "p"
    (p / "runs" / "sim").mkdir(parents=True)
    (p / "runs" / "sim" / "tb.spice").write_text("* tb\n")
    (p / "libs" / "l" / "sim").symlink_to("../../runs/sim")
    with pytest.raises(csmod.UnsupportedLibrary):
        cs.create("p", "T")
    assert not (p / ".git").exists() and not (p / ".gitattributes").exists() and not (p / ".gitignore").exists()
    assert list((p / "changesets").glob("*.json")) == []
    shutil.rmtree(root)


def test_history_set_up_makes_the_first_commit_after_its_git_init():
    root, srv, cs = tc.setup()
    p = root / "p"
    c = TestClient(create_app(srv, None))
    r = c.post("/api/p/p/git/first-commit")                                 # no repository yet
    assert r.status_code == 400 and "not in a git repository" in r.json()["detail"]
    r = c.post("/api/p/p/git/setup")
    body = r.json()
    head = tg.git(p, "rev-parse", "HEAD")
    assert r.status_code == 200 and body["commit"] == head and body["head"]["sha"] == head, r.text
    assert body["init"] is True and body["commit_refused"] is None and body["index_note"] is None
    assert tg.git(p, "log", "-1", "--format=%an|%s") == f"user|{FIRST}" and tg.status(p) == []
    r = c.post("/api/p/p/git/setup")                                        # again: no git init, no commit
    assert r.status_code == 200 and r.json()["commit"] is None and r.json()["init"] is False and subjects(p) == [FIRST]
    r = c.post("/api/p/p/git/first-commit")
    assert r.status_code == 409 and r.json()["code"] == "has_commits"
    meta = cs.create("p", "T")
    assert c.post(f"/api/p/{meta['preview']}/git/first-commit").status_code == 400     # a working copy has none
    shutil.rmtree(root)


def test_the_first_commit_is_refused_in_a_repository_not_set_up_for_undine(tmp_path):
    root = tmp_path / "projects"
    srv = Server(root, {}, [tmp_path])
    srv.store.create("p")
    tg.git(tmp_path, "init", "-q")                                          # e.g. a home directory kept in git
    r = TestClient(create_app(srv, None)).post("/api/p/p/git/first-commit")
    body = r.json()
    assert r.status_code == 400 and body["code"] == "not_set_up" and body["top"] == str(tmp_path)
    assert body["project"] == "p" and body["command"] == f"undine --root {root} git-setup p"
    assert tg.git(tmp_path, "rev-parse", "-q", "--verify", "HEAD", ok=False) == ""


def test_agents_have_no_tool_for_the_first_commit():
    assert not [t["name"] for t in T.TOOLS if "commit" in t["name"]]
