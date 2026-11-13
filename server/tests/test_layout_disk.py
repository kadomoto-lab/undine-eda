"""An open layout (LayoutDB) notices that its cell files changed on disk under it (a git merge, checkout or pull, or
another program). Without unsaved edits it reads them again; with unsaved edits a save is refused, since it would write
the cells as they were before over the merged files, until reload drops the edits and takes the files."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import klayout.db as kdb
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.layouts import layout  # noqa: E402
from undine.layouts.layout import DiskConflict, LayoutDB  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
from undine.store.project import Library  # noqa: E402
from undine.vcs import logmerge  # noqa: E402

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")
A, B, C = "(0,0;100,100)", "(0,0;50,50)", "(200,0;300,100)"     # top on the base, sub on other, top on main


@pytest.fixture(autouse=True)
def git_env(monkeypatch):
    """git without the user's or the system's configuration."""
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def git(d: Path, *a: str) -> str:
    r = subprocess.run(["git", *a], cwd=d, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def boxes(db: LayoutDB, cell: str) -> list[str]:
    li = db.layout.find_layer(68, 20)
    return sorted(str(s.box) for s in db.cell(cell).shapes(li).each()) if li is not None else []


def add(db: LayoutDB, cell: str, box: tuple[int, int, int, int]) -> None:
    with db.recording("create", {"cell": cell}):
        db._commit(cell, [("ins", db.layout.layer(68, 20), kdb.Box(*box))])


def edit(lib: Path, cell: str, box: tuple[int, int, int, int]) -> None:
    """Another writer's edit of a cell file (a branch's, another program's), saved the way Undine saves."""
    db = LayoutDB(Library("t", lib), None)
    add(db, cell, box)
    db.save()


def due(db: LayoutDB) -> LayoutDB:
    """The next look at the disk is not skipped (a layout looks at most once a second)."""
    db._disk_checked -= layout.DISK_CHECK_S
    return db


@pytest.fixture
def repo(tmp_path):
    """Library t in a git repository: top (a box) and sub on the base commit; branch other adds a box to sub; main
    adds one to top since. The operation log is the open layout's own, outside the repository."""
    d, lib = tmp_path / "repo", tmp_path / "repo" / "libs" / "t"
    lib.mkdir(parents=True)
    git(d, "init", "-q", "-b", "main")
    db = LayoutDB(Library("t", lib), None)
    db.new_cell("top")
    db.new_cell("sub")
    add(db, "top", (0, 0, 100, 100))
    db.save()
    git(d, "add", "-A")
    git(d, "commit", "-qm", "base")
    git(d, "checkout", "-q", "-b", "other")
    edit(lib, "sub", (0, 0, 50, 50))
    git(d, "commit", "-qam", "other")
    git(d, "checkout", "-q", "main")
    edit(lib, "top", (200, 0, 300, 100))
    git(d, "commit", "-qam", "main")
    return SimpleNamespace(d=d, lib=lib, log=OpLog(tmp_path / "history" / "t.jsonl"))


def open_db(repo) -> LayoutDB:
    return LayoutDB(Library("t", repo.lib), None, repo.log)


@needs_git
def test_a_clean_layout_reads_the_files_a_git_merge_changed(repo):
    db = open_db(repo)
    v = db.version
    db.tile("top", 0, 0, 0, None, 99)
    git(repo.d, "merge", "-q", "--no-edit", "other")
    db._disk_checked = time.monotonic()
    db.poll_disk()                                       # looked less than a second ago
    assert boxes(db, "sub") == []
    due(db).poll_disk()
    assert boxes(db, "sub") == [B] and boxes(db, "top") == [A, C]
    assert not db.dirty and db.version > v and not db.cache and not db.disk_conflict
    assert db.save()["written"] == [] and git(repo.d, "status", "--porcelain") == ""   # the merge stays
    # a file replaced by another program (no git): the same
    edit(repo.lib, "sub", (0, 100, 50, 150))
    due(db).poll_disk()
    assert boxes(db, "sub") == [B, "(0,100;50,150)"] and not db.dirty


@needs_git
def test_its_own_saves_and_edits_are_no_change_on_disk(repo):
    db = open_db(repo)
    add(db, "top", (0, 500, 10, 510))
    v = db.version
    due(db).poll_disk()
    assert db.version == v and db.dirty and not db.disk_conflict
    db.save()
    due(db).poll_disk()
    assert db.version == v and not db.dirty and boxes(db, "top") == [A, "(0,500;10,510)", C]


@needs_git
def test_unsaved_edits_over_changed_files_refuse_to_save_until_reload(repo):
    db = open_db(repo)
    add(db, "top", (0, 500, 10, 510))                    # unsaved
    git(repo.d, "merge", "-q", "--no-edit", "other")
    due(db).poll_disk()
    assert db.disk_conflict and db.disk_cells == ["sub"]
    assert boxes(db, "sub") == [] and db.dirty           # the edits stay; the files are not read under them
    merged = {p.name: p.read_bytes() for p in repo.lib.iterdir()}
    with pytest.raises(DiskConflict, match=r"the layout files of t changed on disk \(git\?\) while edits were unsaved: "
                                           r"reload to take the files, which drops the unsaved edits"):
        db.save()
    assert {p.name: p.read_bytes() for p in repo.lib.iterdir()} == merged
    assert git(repo.d, "status", "--porcelain") == ""                # the merge was not undone
    add(db, "top", (0, 600, 10, 610))                    # editing goes on
    assert db.disk_conflict
    assert db.reload() == {"dropped": True}
    assert boxes(db, "sub") == [B] and boxes(db, "top") == [A, C]
    assert not db.dirty and not db.disk_conflict and db.disk_cells == []
    assert db.can_undo_redo("top") == (0, 0)            # the dropped edits are not undone on the merged files
    rec = repo.log.read(view="layout")[-1]
    assert rec["op"] == "save" and rec["args"] == {"reload": True} and rec["reason"] == layout.RELOAD_REASON
    assert boxes(open_db(repo), "top") == [A, C]         # nor replayed when the library is opened again
    assert logmerge.journal(repo.log.path.read_bytes()) == []        # nor pending for git (merge, pre-commit hook)
    add(db, "top", (0, 700, 10, 710))
    assert db.can_undo_redo("top") == (1, 0)
    assert db.save()["written"] == ["top"]
    assert boxes(open_db(repo), "sub") == [B]


@needs_git
def test_a_file_touched_with_the_same_content_is_no_conflict(repo):
    """git stash and pop, a copy: the file is written again, its cells are what the layout read."""
    db = open_db(repo)
    add(db, "top", (0, 500, 10, 510))
    p = repo.lib / "sub.oas"
    data = p.read_bytes()
    p.unlink()
    p.write_bytes(data)
    os.utime(p, ns=(p.stat().st_mtime_ns + 10**9, p.stat().st_mtime_ns + 10**9))
    assert layout._stat(p) != db._disk_files["sub"]
    assert not due(db).disk_conflict
    assert db.save()["written"] == ["top"]


@needs_git
def test_a_journal_written_to_the_log_on_disk_is_read(repo):
    """The log changed on disk (git brought a journal: unsaved edits recorded elsewhere). A clean layout replays it,
    as opening the library does; a layout with unsaved edits does not save over it, and reload takes it."""
    db = open_db(repo)
    add(LayoutDB(Library("t", repo.lib), None, repo.log), "top", (0, 900, 10, 910))
    due(db).poll_disk()
    assert boxes(db, "top") == [A, "(0,900;10,910)", C] and db.dirty and not db.disk_conflict
    add(LayoutDB(Library("t", repo.lib), None, repo.log), "sub", (0, 800, 10, 810))
    assert due(db).disk_conflict and db.disk_cells == ["sub"]
    with pytest.raises(DiskConflict):
        db.save()
    assert db.reload() == {"dropped": True}
    assert boxes(db, "sub") == ["(0,800;10,810)"] and boxes(db, "top") == [A, "(0,900;10,910)", C]
    assert not db.disk_conflict and db.save()["written"] == ["sub", "top"]


def test_the_server_refuses_the_save_lists_the_layout_as_stale_and_reloads(tmp_path):
    root = tmp_path / "projects"
    srv = Server(root, {}, [tmp_path], llm_policy="none")
    prj = srv.store.create("p")
    prj.create_library("l")
    lib = prj.path / "libs" / "l"
    first = LayoutDB(Library("l", lib), None)
    first.new_cell("top")
    first.save()
    client = TestClient(create_app(srv, None))
    db = srv.db("p", "l")
    add(db, "top", (0, 500, 10, 510))                    # an unsaved edit, as the editor makes them
    edit(lib, "top", (0, 0, 70, 70))                     # another version of the file (git checkout)
    r = client.post("/api/p/p/l/l/save")
    assert r.status_code == 400 and "changed on disk (git?) while edits were unsaved" in r.json()["detail"]
    assert client.get("/api/p/p/git").json()["stale"] == [{"lib": "l", "cells": ["top"]}]
    r = client.post("/api/p/p/l/l/reload")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == {"dropped": True} and body["dirty"] is False and body["undo"] == [0, 0]
    assert client.get("/api/p/p/git").json()["stale"] == []
    assert boxes(db, "top") == ["(0,0;70,70)"]
    edit(lib, "top", (100, 100, 170, 170))               # clean: the next request reads the files
    due(db)
    info = client.get("/api/p/p/l/l/layout/top").json()
    assert info["version"] > body["version"] and info["bbox"] == [0, 0, 170, 170] and not info["dirty"]
    assert client.post("/api/p/p/l/l/save").status_code == 200


@needs_git
def test_files_that_go_back_end_the_conflict(repo):
    """git checkout away and back (or stash, then pop) under unsaved edits: once the files are again what the layout
    read, saving is allowed again; the conflict is always against what was read or saved, never added up."""
    db = open_db(repo)
    add(db, "top", (0, 500, 10, 510))                    # unsaved
    git(repo.d, "checkout", "-q", "other")
    due(db).poll_disk()
    assert db.disk_conflict and set(db.disk_cells) >= {"top"}
    git(repo.d, "checkout", "-q", "main")
    due(db).poll_disk()
    assert not db.disk_conflict and db.disk_cells == []
    assert db.save()["written"] == ["top"] and boxes(db, "top") == [A, "(0,500;10,510)", C]


@needs_git
def test_undo_never_reaches_behind_files_read_again(repo):
    """A clean layout read again because its files changed (git restore of a saved edit): the log's edits before
    that describe other files, so undo stops there instead of applying them to the new ones (a shape twice)."""
    db = open_db(repo)
    with db.recording("delete", {"cell": "top"}):
        db._commit("top", [("del", db.layout.layer(68, 20), kdb.Box(0, 0, 100, 100))])
    db.save()
    git(repo.d, "checkout", "--", "libs/t/top.oas")    # the saved delete is thrown away on disk
    reads = db.reads
    due(db).poll_disk()
    assert db.reads == reads + 1 and boxes(db, "top") == [A, C]
    assert db.can_undo_redo("top") == (0, 0)
    db.undo("top")
    assert boxes(db, "top") == [A, C]
