"""Changesets on git branches, what review found (docs/design/changeset-branches.md): an edit of the project during an
apply (kept, or a 409 with nothing written), a server killed mid-apply (finished or taken back at the next access,
the log never merged twice), a library the working copy made (closed cleanly), a .gitignore that leaves history/ out
of git (the changeset's records and its symbol checks all the same), a server killed while a changeset is opened or
after its record says closed (the sweep), the review's reads of a long history (parsed once), the baseline read-only
for layout cells and undo, no digests of the cell files at open, and "retained" only while the preview resolves."""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.design import intent as intentmod  # noqa: E402
from undine.schematics import xschem  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store import changeset as csmod  # noqa: E402
from undine.store.changeset import ChangesetError, Changesets, MergeConflict  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
from undine.vcs import branches  # noqa: E402
import test_changeset as tc  # noqa: E402
import test_changeset_git as tg  # noqa: E402
from test_symbol_consumers_api import consumers  # noqa: E402,F401  (the fixture)

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


class Killed(BaseException):
    """The server process dying at that point: nothing after it runs (no `except Exception` sees it)."""


def restart(root: Path) -> tuple[Server, Changesets]:
    srv = Server(root, {}, [root])
    return srv, Changesets(srv)


def mine(srv, cid, project="p", lib="l"):
    """The changeset's records in the project's log, and its merge records: (op, merged_seq)."""
    return [(r["op"], r.get("merged_seq")) for r in tg.log(srv, project, lib) if r.get("changeset") == cid]


def undine_branches(p: Path) -> list[str]:
    return tg.git(p, "for-each-ref", "--format=%(refname)", "refs/heads/undine/").split()


def worktrees(p: Path) -> int:
    return len(tg.git(p, "worktree", "list").splitlines())


# ---------------------------------------------------------------- an edit of the project during an apply


def test_an_intent_edit_during_an_apply_waits_for_it_and_is_kept(monkeypatch):
    """A fresh server has no intent editor open: the person's edit (POST .../intent/edit gets its editor from the
    server) must wait for the merge, not write the file under it and lose to it while its record stays."""
    root, srv, cs, p = tg.committed_project()
    ed = srv.intent_editor("p", "l", "top")
    with ed.recording("add_item", {}, "user", "x"):
        ed.add_item({"kind": "goal", "text": "low noise"}, "user")
    tg.git(p, "add", "-A")
    tg.git(p, "commit", "-qm", "intent")
    srv, cs = restart(root)
    meta = cs.create("p", "Intent", "r", "agent:t")
    cid, P = meta["id"], meta["preview"]
    ed = srv.intent_editor(P, "l", "top")
    with ed.recording("add_item", {}, "agent:t", "y", cid):
        ed.add_item({"kind": "goal", "text": "the changeset's goal"}, "agent:t")

    def person():
        e = srv.intent_editor("p", "l", "top")
        with e.recording("add_item", {}, "user", "mine"):
            e.add_item({"kind": "constraint", "text": "the person's constraint"}, "user")

    real, started = branches.plan_merge, []

    def planning(*a, **k):
        if not started:
            started.append(threading.Thread(target=person))
            started[0].start()
            time.sleep(0.5)                                               # the edit comes while the merge is made
        return real(*a, **k)

    monkeypatch.setattr(csmod.branches, "plan_merge", planning)
    assert cs.apply("p", cid, "user")["status"] == "applied"
    started[0].join(10)
    text = intentmod.path_of(p / "libs" / "l", "top").read_text()
    assert "the person's constraint" in text and "the changeset's goal" in text and "low noise" in text
    recs = tg.log(srv)
    merge = max(i for i, r in enumerate(recs) if r["op"] == "merge")
    assert [(r["op"], r["actor"]) for r in recs[merge + 1:]] == [("add_item", "user")]     # made on the merged file


def test_a_write_that_does_not_wait_for_the_locks_is_a_409_and_nothing_is_written(monkeypatch):
    """The write of a merge is a compare-and-swap: a file changed after the plan read it (another process, a script)
    is not written over; the apply is refused (409 merge_conflict), and applying again merges that edit too."""
    root, srv, cs, p = tg.committed_project()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")
    sch, hist = p / "libs" / "l" / "top.sch", p / "history" / "l.jsonl"
    real = branches.plan_merge

    def elsewhere(*a, **k):
        plan = real(*a, **k)
        if not elsewhere.done:                                            # another process adds R2, after the plan
            f = xschem.parse(sch.read_text())
            f.comps.append(xschem.XComp("r.sym", 200, 0, 0, 0, {"name": "R2", "value": "5k"}))
            sch.write_text(xschem.dump(f))
            elsewhere.done = True
        return plan

    elsewhere.done = False
    monkeypatch.setattr(csmod.branches, "plan_merge", elsewhere)
    log_before = hist.read_bytes() if hist.is_file() else None
    r = c.post(f"/api/p/p/changesets/{cid}/apply")
    body = r.json()
    assert r.status_code == 409 and body["code"] == "merge_conflict", r.text
    assert "changed in the project while the merge was being made" in body["detail"]
    assert "nothing was written" in body["detail"]
    assert [f["path"] for f in body["files"]] == ["libs/l/top.sch"] and body["files"][0]["kind"] == "schematic"
    other = sch.read_text()
    assert "R2" in other and "10k" not in other                           # the other edit stays, nothing merged
    assert (hist.read_bytes() if hist.is_file() else None) == log_before
    assert cs.get("p", cid)["status"] == "open" and not (p / "changesets" / cid / "merging.json").exists()
    out = cs.apply("p", cid, "user")                                      # again: merged with that edit
    assert out["status"] == "applied" and tc.value(srv) == "10k" and "R2" in sch.read_text()
    assert mine(srv, cid) == [("set_props", 1), ("merge", None)]


# ---------------------------------------------------------------- a server killed mid-apply


def test_killed_after_the_merge_was_written_the_next_access_finishes_the_apply(monkeypatch):
    root, srv, cs, p = tg.committed_project()
    head = tg.git(p, "rev-parse", "HEAD")
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")

    def die(*a, **k):
        raise Killed()

    with monkeypatch.context() as m:
        m.setattr(csmod.branches, "commit_merge", die)
        with pytest.raises(Killed):
            cs.apply("p", cid, "user")
    assert (p / "changesets" / cid / "merging.json").is_file()
    srv, cs = restart(root)
    got = cs.get("p", cid)
    a = got["applied"]
    assert got["status"] == "applied" and a["commit"] is None and a["uncommitted"] is True
    assert a["why"] == "failed"                                           # Undine stopped before it committed
    assert set(a["paths"]) >= {"history/l.jsonl", "libs/l/top.sch"} and a["tip"] == got["git"]["tip"]
    assert tc.value(srv) == "10k" and mine(srv, cid) == [("set_props", 1), ("merge", None)]     # merged once
    assert not (p / "changesets" / cid / "merging.json").exists()
    # the closure finished too: the branch kept, the worktrees gone
    assert got["git"]["ref"] == csmod.kept_ref("p", cid) and undine_branches(p) == [] and worktrees(p) == 1
    assert tg.git(p, "rev-parse", "HEAD") == head
    with pytest.raises(ChangesetError, match="applied, not open"):
        cs.apply("p", cid, "user")
    with pytest.raises(ChangesetError, match="applied, not open"):
        cs.discard("p", cid)
    assert mine(srv, cid) == [("set_props", 1), ("merge", None)]


def test_killed_before_the_record_was_saved_the_merge_commit_is_found_not_made_again(monkeypatch):
    root, srv, cs, p = tg.committed_project()
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")
    real = Changesets._save

    def save(self, project, m):
        if m.get("status") == "applied":
            raise Killed()                                                # e.g. a full disk, or the process killed
        return real(self, project, m)

    with monkeypatch.context() as m:
        m.setattr(Changesets, "_save", save)
        with pytest.raises(Killed):
            cs.apply("p", cid, "user")
    merge = tg.git(p, "rev-parse", "HEAD")
    assert tg.git(p, "log", "-1", "--format=%s").startswith(f"Apply {cid}")
    srv, cs = restart(root)
    got = cs.get("p", cid)
    assert got["status"] == "applied" and got["applied"]["commit"] == merge and got["applied"]["uncommitted"] is False
    assert got["applied"]["why"] is None
    assert tg.git(p, "rev-parse", "HEAD") == merge and tg.git(p, "rev-list", "--count", "--merges", "HEAD") == "1"
    assert mine(srv, cid) == [("set_props", 1), ("merge", None)] and tc.value(srv) == "10k"


def test_killed_while_the_files_were_renamed_in_the_merge_is_taken_back(monkeypatch):
    root, srv, cs, p = tg.committed_project()
    srv.oplog("p", "l").append({"op": "note", "actor": "user", "cell": None, "view": None, "effects": []})
    tg.git(p, "add", "-A")
    tg.git(p, "commit", "-qm", "a log")                                   # the project's log: seq 1
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")
    files = {f: f.read_bytes() for f in (p / "libs").rglob("*") if f.is_file()}
    hist = p / "history" / "l.jsonl"
    log_before = hist.read_bytes()

    def torn(d, plan):                                   # write_plan's own steps, killed after its first rename
        writes, token = sorted(plan["writes"].items()), "0123456789ab"
        for rel, data in writes:
            f = Path(d) / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.with_name(f".{f.name}.{token}.new").write_bytes(data)
        f = Path(d) / writes[0][0]
        assert writes[0][0] == "history/l.jsonl" and f.is_file()         # the log first: the merge's record in
        os.replace(f, f.with_name(f".{f.name}.{token}.old"))
        os.replace(f.with_name(f".{f.name}.{token}.new"), f)
        raise Killed()

    with monkeypatch.context() as m:
        m.setattr(csmod.branches, "write_plan", torn)
        with pytest.raises(Killed):
            cs.apply("p", cid, "user")
    assert b'"op":"merge"' in hist.read_bytes()
    srv, cs = restart(root)
    assert cs.get("p", cid)["status"] == "open"
    assert hist.read_bytes() == log_before and tc.value(srv) == "1k"
    assert {f: f.read_bytes() for f in (p / "libs").rglob("*") if f.is_file()} == files      # no .new/.old left
    assert not list(p.glob("history/.*")) and not (p / "changesets" / cid / "merging.json").exists()
    assert cs.apply("p", cid, "user")["status"] == "applied" and tc.value(srv) == "10k"
    assert mine(srv, cid) == [("set_props", 2), ("merge", None)]


def torn_after(renames: int):
    """write_plan's own steps (branches.write_plan: every new file staged, then renamed in, in order), the server
    killed after `renames` of them: here, the log, then the netlist, then the schematic."""
    def torn(d, plan):
        writes, token = sorted(plan["writes"].items()), "0123456789ab"
        assert [w for w, _ in writes] == ["history/l.jsonl", "libs/l/top.netlist.yaml", "libs/l/top.sch"]
        for rel, data in writes:
            f = Path(d) / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.with_name(f".{f.name}.{token}.new").write_bytes(data)
        for rel, _ in writes[:renames]:
            f = Path(d) / rel
            if f.exists():
                os.replace(f, f.with_name(f".{f.name}.{token}.old"))
            os.replace(f.with_name(f".{f.name}.{token}.new"), f)
        raise Killed()
    return torn


def killed_mid_rename(monkeypatch, renames: int = 2, log: bool = False):
    """A changeset setting R1 to 10k, its apply killed after the log and the netlist were renamed in: the
    schematic is still the project's (1k), its new one beside it. `log`: the project has a log (a note) before."""
    root, srv, cs, p = tg.committed_project()
    if log:
        srv.oplog("p", "l").append({"op": "note", "actor": "user", "cell": None, "view": None, "effects": []})
        tg.git(p, "add", "-A")
        tg.git(p, "commit", "-qm", "a log")
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")
    with monkeypatch.context() as m:
        m.setattr(csmod.branches, "write_plan", torn_after(renames))
        with pytest.raises(Killed):
            cs.apply("p", cid, "user")
    assert b'"op":"merge"' in (p / "history" / "l.jsonl").read_bytes() and tc.value(srv) == "1k"
    from undine.schematics import netlist
    netlist._SYNCED.clear()                           # a new process: nothing read yet
    netlist._MEMO.clear()
    csmod._PARSED.clear()
    return root, p, cid


def test_killed_mid_rename_the_watchers_record_does_not_leave_the_merge_in_the_log(monkeypatch):
    """The netlist watcher's first scan sees the schematic differ from the netlist the merge renamed in and appends
    its record to the merged log: that log is still the merge's (it starts with it), the schematic is not, so the
    merge is taken back, the watcher's record kept, and an apply again merges the changeset's records once."""
    from undine.schematics import netlist
    root, p, cid = killed_mid_rename(monkeypatch)
    srv, cs = restart(root)
    assert [Path(x).name for x in netlist.Watcher(srv).scan()] == ["top.sch"]
    assert [r["op"] for r in tg.log(srv)][-3:] == ["set_props", "merge", "import_drawing"]
    got = cs.get("p", cid)
    assert got["status"] == "open" and tc.value(srv) == "1k" and mine(srv, cid) == []
    assert [r["op"] for r in tg.log(srv)] == ["import_drawing"]                   # the watcher's, kept
    assert "taken back" in got["cleanup_warning"] and "nothing of it is in the project" in got["interrupted"]
    assert not list(p.glob("**/.*.new")) and not list(p.glob("**/.*.old"))
    out = cs.apply("p", cid, "user")
    assert out["status"] == "applied" and tc.value(srv) == "10k" and mine(srv, cid) == [("set_props", 1), ("merge", None)]
    assert "interrupted" not in out and out.get("cleanup_warning") is None


def test_killed_mid_rename_an_edit_is_not_taken_for_a_finished_merge(monkeypatch):
    """The person edits the schematic the merge had not renamed in yet: the merge is not finished (it would say
    applied with R1 still 1k), it is taken back, the person's edit and its record kept."""
    root, p, cid = killed_mid_rename(monkeypatch)
    srv, cs = restart(root)
    ed = srv.editor("p", "l", "top", "schematic")
    with ed.recording("set_props", {"id": "R1"}, "user", "mine"):
        ed.set_props("i", "R1", {"m": "2"})
    got = cs.get("p", cid)
    assert got["status"] == "open" and tc.value(srv) == "1k" and mine(srv, cid) == []
    assert [r["op"] for r in tg.log(srv)] == ["set_props"] and "nothing of it" in got["cleanup_warning"]
    assert "props: {m: '2', value: 1k}" in (p / "libs" / "l" / "top.netlist.yaml").read_text()   # the schematic's
    out = cs.apply("p", cid, "user")
    props = srv.editor("p", "l", "top", "schematic").instance("R1")["props"]
    assert out["status"] == "applied" and props["value"] == "10k" and props["m"] == "2"
    assert mine(srv, cid) == [("set_props", 1), ("merge", None)]


def test_killed_mid_rename_the_log_is_put_back_without_the_file_it_moved_aside(monkeypatch):
    """The merged log starts with the project's: taking it back needs no ".old" file (one removed by hand)."""
    root, p, cid = killed_mid_rename(monkeypatch, log=True)
    (old,) = p.glob("history/.l.jsonl.*.old")
    old.unlink()
    srv, cs = restart(root)
    assert cs.get("p", cid)["status"] == "open" and mine(srv, cid) == [] and [r["op"] for r in tg.log(srv)] == ["note"]
    assert cs.apply("p", cid, "user")["status"] == "applied" and mine(srv, cid) == [("set_props", 2), ("merge", None)]


def test_killed_after_the_merge_was_written_an_edit_since_does_not_take_it_back(monkeypatch):
    """write_plan finished (merging.json says so), the server killed before the netlists were read again: the merge
    is all in the project, its .old files gone. An edit made before the restart does not make it look half done:
    it is finished, never left open with its records in the log (they would be merged twice)."""
    root, srv, cs, p = tg.committed_project()
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")

    def die(*a, **k):
        raise Killed()

    with monkeypatch.context() as m:
        m.setattr(Changesets, "_sync_netlists", die)
        with pytest.raises(Killed):
            cs.apply("p", cid, "user")
    assert json.loads((p / "changesets" / cid / "merging.json").read_text())["written"] is True
    srv, cs = restart(root)
    sch = p / "libs" / "l" / "top.sch"                                    # another program edits it meanwhile
    sch.write_text(sch.read_text().replace("value=10k", "value=10k m=3"))
    got = cs.get("p", cid)
    assert got["status"] == "applied" and mine(srv, cid) == [("set_props", 1), ("merge", None)]
    assert "m=3" in sch.read_text() and "value=10k" in sch.read_text()


def test_serve_finishes_an_interrupted_merge_before_the_netlist_watcher_starts(monkeypatch, tmp_path):
    """`undine serve` takes the interrupted merge back before its netlist watcher (and the API) start, so the
    watcher's first scan finds a project the merge is out of, and records nothing."""
    import uvicorn
    from undine import __main__ as cli
    from undine.server import api
    root, p, cid = killed_mid_rename(monkeypatch)
    seen = []

    def watch(self, interval=1.0):
        seen.append((p / "changesets" / cid / "merging.json").exists())
        from undine.schematics import netlist
        netlist.Watcher(self).scan()                                      # its first scan, at once

    def serve(self, *a, **k):                                             # the API, as if it ran and stopped
        seen.append("api")
        self.started = True

    monkeypatch.setattr(api.Server, "watch_netlists", watch)
    monkeypatch.setattr(uvicorn.Server, "run", serve)
    monkeypatch.delenv("UNDINE_MCP", raising=False)
    (tmp_path / "pdks").mkdir()
    monkeypatch.setattr(sys, "argv", ["undine", "--root", str(root), "--pdk-root", str(tmp_path / "pdks"), "serve"])
    cli.main()
    assert seen == [False, "api"]
    srv, cs = restart(root)
    assert tg.log(srv) == [] and cs.get("p", cid)["status"] == "open"
    assert cs.apply("p", cid, "user")["status"] == "applied" and mine(srv, cid) == [("set_props", 1), ("merge", None)]


def test_killed_in_the_revert_commit_a_commit_of_the_persons_is_not_taken_for_it(monkeypatch):
    """A revert killed in its commit: the merge is written, not committed. The person commits something else
    before Undine is started again; that commit has the revert's parent, but not its subject nor its committer."""
    root, srv, cs, p = tg.committed_project()
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")
    cs.apply("p", cid, "user")

    def die(*a, **k):
        raise Killed()

    with monkeypatch.context() as m:
        m.setattr(csmod.branches, "commit_merge", die)
        with pytest.raises(Killed):
            cs.revert("p", cid, "user")
    (p / "NOTES.txt").write_text("my notes\n")
    tg.git(p, "add", "NOTES.txt")
    tg.git(p, "commit", "-qm", "my notes")
    srv, cs = restart(root)
    got = cs.get("p", cid)
    r = got["reverted"]
    assert got["status"] == "reverted" and r["commit"] is None and r["uncommitted"] is True and r["why"] == "failed"
    assert {"history/l.jsonl", "libs/l/top.sch"} <= set(r["paths"]) and tc.value(srv) == "1k"
    assert tg.git(p, "log", "-1", "--format=%s") == "my notes"


def test_killed_in_the_revert_commit_its_own_commit_is_found(monkeypatch):
    """The same, killed after the commit: the commit found is Undine's, with the revert's subject."""
    root, srv, cs, p = tg.committed_project()
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")
    cs.apply("p", cid, "user")
    real = Changesets._save

    def save(self, project, m):
        if m.get("status") == "reverted":
            raise Killed()
        return real(self, project, m)

    with monkeypatch.context() as m:
        m.setattr(Changesets, "_save", save)
        with pytest.raises(Killed):
            cs.revert("p", cid, "user")
    commit = tg.git(p, "rev-parse", "HEAD")
    srv, cs = restart(root)
    r = cs.get("p", cid)["reverted"]
    assert r["commit"] == commit and r["uncommitted"] is False and r["why"] is None
    assert tg.git(p, "log", "-1", "--format=%s %cn") == f"Revert {cid}: Raise R1 Undine"


# ---------------------------------------------------------------- what a revert keeps, what a refused apply puts back


def test_a_revert_keeps_a_library_the_changeset_made_and_the_work_done_in_it_since():
    """The revert keeps the files the changeset added (the cell it made in library m), so it keeps m in project.json
    too: else the person's cell made in m since, and the kept one, would no longer be in the project."""
    root, srv, cs, p = tg.committed_project()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "New library", "r", "agent:t")
    cid, P = meta["id"], meta["preview"]
    assert c.post(f"/api/p/{P}/libs", json={"name": "m"}).status_code == 200
    (srv.project(P).library("m").path / "r.sym").write_text(tc.R_SYM)
    ed = srv.editor(P, "m", "a", "schematic")
    with ed.recording("create_cell", {}, "agent:t", "new", cid):
        ed.create_file("")
        ed.add_instance("r.sym", 0, 0)
    tg.set_r1(srv, P, "10k", cid, "agent:t")
    cs.apply("p", cid, "user")
    assert sorted(srv.project("p").library_specs()) == ["l", "m"]
    ed = srv.editor("p", "m", "b", "schematic")                           # the person's work in m since
    with ed.recording("create_cell", {}, "user", "mine"):
        ed.create_file("")
        ed.add_instance("r.sym", 0, 0)
    tg.git(p, "add", "-A")
    tg.git(p, "commit", "-qm", "cell b")
    project_json = (p / "project.json").read_bytes()
    r = c.post(f"/api/p/p/changesets/{cid}/revert")
    assert r.status_code == 200 and r.json()["status"] == "reverted" and tc.value(srv) == "1k"
    assert sorted(srv.project("p").library_specs()) == ["l", "m"] and (p / "project.json").read_bytes() == project_json
    cells = c.get("/api/p/p/l/m/cells")
    assert cells.status_code == 200 and {"a", "b"} <= {x["name"] for x in cells.json()}
    assert "project.json" not in r.json()["reverted"]["paths"] and tg.status(p) == []


def test_a_revert_takes_out_a_library_the_changeset_made_that_has_no_file_left():
    root, srv, cs, p = tg.committed_project()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "New library", "r", "agent:t")
    cid, P = meta["id"], meta["preview"]
    assert c.post(f"/api/p/{P}/libs", json={"name": "m"}).status_code == 200
    (srv.project(P).library("m").path / "r.sym").write_text(tc.R_SYM)
    cs.apply("p", cid, "user")
    (p / "libs" / "m" / "r.sym").unlink()                                 # the person removed what it added
    tg.git(p, "add", "-A")
    tg.git(p, "commit", "-qm", "no r.sym")
    assert cs.revert("p", cid, "user")["status"] == "reverted" and sorted(srv.project("p").library_specs()) == ["l"]


def test_a_refused_apply_puts_back_only_what_its_save_wrote(monkeypatch):
    """The project's unsaved layout edit is saved before the merge; another process then writes a record into the
    log and a layout cell file: the compare-and-swap refuses the apply (409), and putting the save back keeps both."""
    from undine.layouts import layout, layoutstore
    import klayout.db as kdb
    root, srv, cs, p = tg.committed_project()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")
    db = srv.db("p", "l")
    with db.recording("create", {}, "user", "box"):
        db._commit("top", [("ins", db.layout.layer(68, 20), kdb.Box(0, 0, 100, 100))])
    assert db.dirty
    hist, theirs = p / "history" / "l.jsonl", p / "libs" / "l" / layoutstore.file_name("theirs")
    top = p / "libs" / "l" / layoutstore.file_name("top")
    top_before = top.read_bytes()
    real, calls = branches.plan_merge, []

    def plan(*a, **k):
        out = real(*a, **k)
        calls.append(1)
        if len(calls) == 2:                                               # after the save: another process writes
            last = json.loads(hist.read_text().splitlines()[-1])["seq"]
            with hist.open("a") as fh:
                fh.write(json.dumps({"v": 1, "seq": last + 1, "op": "note", "actor": "other-process", "cell": None,
                                     "view": None, "effects": []}) + "\n")
            ly = kdb.Layout()
            ly.create_cell("theirs").shapes(ly.layer(68, 20)).insert(kdb.Box(0, 0, 50, 50))
            ly.write(str(theirs))
        return out

    monkeypatch.setattr(csmod.branches, "plan_merge", plan)
    r = c.post(f"/api/p/p/changesets/{cid}/apply")
    assert r.status_code == 409 and r.json()["code"] == "merge_conflict" and "nothing was written" in r.json()["detail"]
    recs = tg.log(srv)
    assert [(x["op"], x["actor"]) for x in recs] == [("create", "user"), ("note", "other-process")]
    assert theirs.is_file() and top.read_bytes() == top_before
    assert [x["op"] for _, x in layout._journal(recs)[1]] == ["create"]   # the unsaved edit: a journal again
    assert db.dirty and tg.boxes(db) == ["(0,0;100,100)"]
    assert cs.get("p", cid)["status"] == "open" and tc.value(srv) == "1k"


# ---------------------------------------------------------------- opening: what is refused, and said


def test_a_library_no_worktree_can_hold_is_refused_before_the_project_is_made_a_repository(tmp_path):
    root, srv, cs = tc.setup()
    p = root / "p"
    outside = tmp_path / "outside" / "shared"
    outside.mkdir(parents=True)
    (outside / "amp.sch").write_text("v {xschem version=3.4.5 file_version=1.2}\n")
    prj = srv.project("p")
    prj.meta.setdefault("libraries", {})["shared"] = "libs/shared"
    prj.save()
    os.symlink(outside, p / "libs" / "shared")
    with pytest.raises(csmod.UnsupportedLibrary) as refused:
        cs.create("p", "T")
    e = refused.value
    assert (e.library, e.path, e.kind, e.target) == ("shared", "libs/shared", "link_out", str(outside))
    r = TestClient(create_app(srv, None)).post("/api/p/p/changesets", json={"title": "T", "reason": ""})
    body = r.json()
    assert r.status_code == 400 and body["code"] == "unsupported_library", r.text
    assert {k: body[k] for k in ("library", "path", "kind", "target")} == \
        {"library": "shared", "path": "libs/shared", "kind": "link_out", "target": str(outside)}
    assert body["detail"].startswith("cannot open a changeset: library shared: libs/shared is a symbolic link")
    # refused before git init: the project is in no repository still, and nothing of a changeset was made
    assert not (p / ".git").exists() and not (p / ".gitattributes").exists() and not (p / ".gitignore").exists()
    assert not (root / ".changesets").exists() and list((p / "changesets").glob("*.json")) == []


def test_a_refusal_only_the_tree_shows_takes_back_the_repository_git_setup_made():
    """The link is fine as a folder; only the tree built after git init shows it leads to what git does not keep
    (runs/, ignored): the open is refused and the project is in no repository again, its own .gitignore as it was."""
    root, srv, cs = tc.setup()
    p = root / "p"
    (p / "runs" / "sim").mkdir(parents=True)
    (p / "runs" / "sim" / "tb.spice").write_text("* tb\n")
    os.symlink("../../runs/sim", p / "libs" / "l" / "sim")
    (p / ".gitignore").write_text("*.bak\n")
    with pytest.raises(csmod.UnsupportedLibrary) as refused:
        cs.create("p", "T")
    assert (refused.value.library, refused.value.kind) == ("l", "link_nowhere")
    assert not (p / ".git").exists() and not (p / ".gitattributes").exists()
    assert (p / ".gitignore").read_text() == "*.bak\n"
    assert list((p / "changesets").glob("*.json")) == []


def test_a_library_outside_the_project_is_read_only_in_the_working_copy(tmp_path):
    """An absolute path in project.json is no folder of the worktree: the working copy would write straight into the
    real library, so there it is read-only, as a dependency is."""
    root, srv, cs = tc.setup()
    outside = tmp_path / "shared"
    outside.mkdir()
    (outside / "amp.sch").write_text("v {xschem version=3.4.5 file_version=1.2}\n")
    prj = srv.project("p")
    prj.meta.setdefault("libraries", {})["shared"] = str(outside)
    prj.save()
    meta = cs.create("p", "T")
    assert srv.project("p").libraries()["shared"].readonly is False
    w = srv.project(meta["preview"])
    assert isinstance(w, csmod.Working)
    libs = w.libraries()
    assert libs["shared"].readonly is True and libs["shared"].path == outside
    assert libs["l"].readonly is False and libs["l"].path.is_relative_to(w.path)
    api = TestClient(create_app(srv, None))
    r = api.post(f"/api/p/{meta['preview']}/l/shared/cells", json={"name": "buf", "views": ["schematic"]})
    assert r.status_code >= 400 and not (outside / "buf.sch").exists(), r.text
    r = api.post("/api/p/p/l/shared/cells", json={"name": "buf", "views": ["schematic"]})      # the project's own: writable
    assert r.status_code == 200 and (outside / "buf.sch").exists(), r.text


def test_a_merge_another_server_still_runs_is_not_taken_for_an_interrupted_one(monkeypatch):
    """A restart starts the new server while the old one may still finish an apply: its lock on the changeset (the
    kernel's, released when it ends) keeps the new one from finishing or taking back the merge under it."""
    import subprocess
    root, srv, cs, p = tg.committed_project()
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")

    def die(*a, **k):
        raise Killed()

    with monkeypatch.context() as m:
        m.setattr(csmod.branches, "commit_merge", die)
        with pytest.raises(Killed):
            cs.apply("p", cid, "user")
    merging = p / "changesets" / cid / "merging.json"
    assert merging.is_file()
    lock = csmod._lock_file(cs._shadow_root("p", cid))
    old = subprocess.Popen([sys.executable, "-c", "import fcntl, os, sys\n"
                            "fd = os.open(sys.argv[1], os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_EX)\n"
                            "print('held', flush=True); sys.stdin.read()", str(lock)],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert old.stdout.readline().strip() == "held"
        srv, cs = restart(root)
        assert cs.finish_interrupted() == [] and merging.is_file()
        assert cs.get("p", cid)["status"] == "open" and merging.is_file()
    finally:
        old.stdin.close()
        old.wait(timeout=10)
    got = cs.get("p", cid)                        # the old server is gone: the next access finishes it
    assert got["status"] == "applied" and not merging.exists()
    assert tc.value(srv) == "10k" and mine(srv, cid) == [("set_props", 1), ("merge", None)]


# ---------------------------------------------------------------- a library the working copy made


@pytest.mark.parametrize("action", ["apply", "discard"])
def test_closing_a_changeset_that_made_a_library_closes_it_cleanly(action):
    root, srv, cs, p = tg.committed_project()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "New library", "r", "agent:t")
    cid, P = meta["id"], meta["preview"]
    assert c.post(f"/api/p/{P}/libs", json={"name": "m"}).status_code == 200
    (srv.project(P).library("m").path / "r.sym").write_text(tc.R_SYM)
    ed = srv.editor(P, "m", "a", "schematic")
    with ed.recording("create_cell", {}, "agent:t", "new", cid):
        ed.create_file("")
        ed.add_instance("r.sym", 0, 0)
    out = getattr(cs, action)("p", cid, *(["user"] if action == "apply" else []))
    got = cs.get("p", cid)
    assert out.get("cleanup_warning") is None and got.get("cleanup_warning") is None and got["retained"] is False
    assert got["git"]["ref"] == csmod.kept_ref("p", cid) and undine_branches(p) == [] and worktrees(p) == 1
    assert not (root / ".changesets" / "p" / cid).exists() and "m" in got["libs"]
    assert [x["lib"] for x in got["changes"]] == ["m"]
    if action == "apply":
        ops = [op for op, _ in mine(srv, cid, lib="m")]
        assert (p / "libs" / "m" / "a.sch").is_file() and ops[-1] == "merge" and set(ops[:-1]) == {"create_cell"}


# ---------------------------------------------------------------- a .gitignore that leaves history/ out of git


def ignored_history(consumers):
    from undine.__main__ import git_setup
    p = consumers.prj.path
    git_setup(p, say=None)
    with open(p / ".gitignore", "a") as f:
        f.write("history/\n")                                             # the person keeps the logs out of git
    tg.git(p, "add", "-A")
    tg.git(p, "commit", "-qm", "project")
    return p


@pytest.mark.parametrize("operation", ["apply", "revert"])
def test_with_history_ignored_a_late_consumer_still_blocks_the_closure(consumers, operation):
    from test_symbol_consumers_api import _consumer, _files, _logs, _move
    p = ignored_history(consumers)
    cs = Changesets(consumers.srv)
    meta = cs.create("p", "Move pin A")
    _move(consumers, meta["preview"])
    if operation == "revert":
        cs.apply("p", meta["id"])
    model = xschem.parse((consumers.design.path / "block.sym").read_text())
    (consumers.other.path / "late.sch").write_text(xschem.dump(_consumer("design/block.sym", "LATE", model)))
    before, logs = _files(consumers), _logs(consumers)
    with pytest.raises(MergeConflict, match="late"):
        getattr(cs, operation)("p", meta["id"])
    assert _files(consumers) == before and _logs(consumers) == logs
    assert tg.git(p, "ls-tree", "-r", "--name-only", "HEAD", "history") == ""


def test_with_history_ignored_the_changesets_records_reach_the_project_log(consumers):
    from test_symbol_consumers_api import _move
    p = ignored_history(consumers)
    cs = Changesets(consumers.srv)
    meta = cs.create("p", "Move pin A")
    cid = meta["id"]
    _move(consumers, meta["preview"])
    out = cs.apply("p", cid)
    assert out["status"] == "applied" and out["applied"]["commit"] == tg.git(p, "rev-parse", "HEAD")
    recs = [r for r in consumers.srv.oplog("p", "design").read(limit=100) if r.get("changeset") == cid]
    assert [r["op"] for r in recs][-1] == "merge" and len(recs) > 1 and all(r.get("merged_seq") for r in recs[:-1])
    # the log stays out of the user's branch, as their .gitignore says; the tree is the commit's
    assert tg.git(p, "ls-tree", "-r", "--name-only", "HEAD", "history") == "" and tg.status(p) == []
    assert cs.revert("p", cid)["status"] == "reverted"


# ---------------------------------------------------------------- a server killed while opening, or after closing


def test_killed_after_the_record_said_applied_the_sweep_closes_it(monkeypatch):
    root, srv, cs, p = tg.committed_project()
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "10k", cid, "agent:t")

    def die(*a, **k):
        raise Killed()

    with monkeypatch.context() as m:
        m.setattr(Changesets, "_cleanup", die)
        with pytest.raises(Killed):
            cs.apply("p", cid, "user")
    assert undine_branches(p) == [f"refs/heads/undine/p/{cid}"] and worktrees(p) == 3
    srv, cs = restart(root)
    assert [(x["id"], x["status"]) for x in cs.list("p")] == [(cid, "applied")]     # the first access sweeps
    assert undine_branches(p) == [] and worktrees(p) == 1 and not (root / ".changesets" / "p" / cid).exists()
    got = cs.get("p", cid)
    assert got["git"]["ref"] == csmod.kept_ref("p", cid) and got.get("cleanup_warning") is None
    assert "value=10k" in tg.git(p, "show", f"{got['git']['ref']}:libs/l/top.sch")


def test_killed_while_opening_the_sweep_keeps_the_branch_and_removes_the_worktrees(monkeypatch):
    root, srv, cs, p = tg.committed_project()

    def die(*a, **k):
        raise Killed()

    with monkeypatch.context() as m:
        m.setattr(Changesets, "_save", die)
        with pytest.raises(Killed):
            cs.create("p", "Raise R1", "r", "agent:t")
    (cid,) = [d.name for d in (root / ".changesets" / "p").iterdir() if d.is_dir()]      # not its lock file
    assert undine_branches(p) == [f"refs/heads/undine/p/{cid}"] and worktrees(p) == 3
    srv, cs = restart(root)
    assert cs.list("p") == []
    assert undine_branches(p) == [] and worktrees(p) == 1 and not (root / ".changesets" / "p" / cid).exists()
    kept = csmod.kept_ref("p", cid)                                       # never deleted: kept out of the way
    assert tg.git(p, "rev-parse", f"{kept}^{{tree}}") == tg.git(p, "rev-parse", "HEAD^{tree}")
    assert cs.create("p", "Again")["status"] == "open"


def test_a_working_copy_its_closure_retained_is_left_alone_and_offered(monkeypatch):
    root, srv, cs, p = tg.committed_project()
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid, P = meta["id"], meta["preview"]
    tg.set_r1(srv, P, "10k", cid, "agent:t")
    real = Changesets._archive

    def late(self, project, m):
        out = real(self, project, m)
        tg.set_r1(srv, P, "11k", cid, "agent:t")                          # an edit after the evidence was saved
        return out

    with monkeypatch.context() as m:
        m.setattr(Changesets, "_archive", late)
        cs.apply("p", cid, "user")
    got = cs.get("p", cid)
    assert got["cleanup_warning"] and got["retained"] is True and tc.value(srv) == "10k"
    srv, cs = restart(root)
    got = cs.get("p", cid)
    assert got["retained"] is True and undine_branches(p) == [f"refs/heads/undine/p/{cid}"] and worktrees(p) == 3
    assert tc.value(srv, P) == "11k"


def test_retained_is_offered_only_while_the_preview_resolves():
    """A branch that could not be kept: the worktrees are gone, so there is no preview to open; a later sweep keeps
    the branch once it can."""
    root, srv, cs, p = tg.committed_project()
    meta = cs.create("p", "Try", "r", "agent:t")
    cid = meta["id"]
    tg.set_r1(srv, meta["preview"], "5k", cid, "agent:t")
    tg.git(p, "update-ref", csmod.kept_ref("p", cid), "HEAD")             # in the way
    cs.discard("p", cid)
    got = cs.get("p", cid)
    assert "could not be kept" in got["cleanup_warning"] and got["retained"] is False
    assert TestClient(create_app(srv, None)).get(f"/api/p/{meta['preview']}/git").status_code == 404
    tg.git(p, "update-ref", "-d", csmod.kept_ref("p", cid))
    srv, cs = restart(root)
    got = cs.get("p", cid)
    assert got.get("cleanup_warning") is None and got["git"]["ref"] == csmod.kept_ref("p", cid)
    assert undine_branches(p) == [] and "value=5k" in tg.git(p, "show", f"{got['git']['ref']}:libs/l/top.sch")


# ---------------------------------------------------------------- the review's reads of a long history


def test_the_working_copys_records_are_parsed_once_not_the_whole_history_each_poll(monkeypatch):
    root, srv, cs = tc.setup()
    hist = root / "p" / "history" / "l.jsonl"
    hist.parent.mkdir(exist_ok=True)
    hist.write_text("".join(json.dumps({"v": 1, "seq": i, "t": "2026-01-01T00:00:00+0000", "op": "note",
                                        "actor": "user", "cell": "top", "view": "schematic", "effects": []},
                                       separators=(",", ":")) + "\n" for i in range(1, 2001)))   # the project's history
    meta = cs.create("p", "Raise R1", "r", "agent:t")
    cid, P = meta["id"], meta["preview"]
    assert meta["git"]["base_seq"] == {"l": 2000}
    for v in ("2k", "3k"):
        tg.set_r1(srv, P, v, cid, "agent:t")
    c = TestClient(create_app(srv, None))
    c.get(f"/api/p/p/changesets/{cid}")                                   # its layout opened, its log read once
    path = srv.oplog(P, "l").path
    full, parsed = [], []
    real_read, real_parse = OpLog._read_all, csmod._parse_lines
    monkeypatch.setattr(OpLog, "_read_all", lambda self: full.append(self.path) or real_read(self))
    monkeypatch.setattr(csmod, "_parse_lines", lambda data, floor: parsed.extend(
        x for x in data.split(b"\n") if x.strip()) or real_parse(data, floor))
    for _ in range(3):
        assert c.get(f"/api/p/p/changesets/{cid}/activity?after=0").json()["last"] == 2
        assert [o["first"] for o in c.get(f"/api/p/p/changesets/{cid}/timeline").json()["ops"]] == [1, 2]
        assert [o["op"] for x in c.get(f"/api/p/p/changesets/{cid}").json()["changes"] for o in x["ops"]] == \
            ["set_props", "set_props"]
    assert path not in full and parsed == []                              # nothing parsed again
    csmod._forget_parsed(root / ".changesets")
    assert [r["seq"] for r in csmod._records_after(path, 2000)] == [2001, 2002] and len(parsed) == 2   # bisection
    parsed.clear()
    tg.set_r1(srv, P, "4k", cid, "agent:t")
    full.clear()
    assert c.get(f"/api/p/p/changesets/{cid}/activity?after=0").json()["last"] == 3
    assert path not in full and len(parsed) == 1                          # the line appended, not the history


def test_records_after_reads_as_the_log_does(tmp_path):
    f = tmp_path / "l.jsonl"
    log = OpLog(f)
    for i in range(50):
        log.append({"op": "note", "i": i})
    assert csmod._records_after(f, 40) == OpLog(f, None, 40).own() and len(OpLog(f, None, 40).own()) == 10
    assert csmod._records_after(f, 0) == log.own()
    log.append({"op": "note", "i": 50})
    with f.open("a") as fh:
        fh.write('{"v":1,"seq":52,"op":"no')                              # a line being written
    assert [r["seq"] for r in csmod._records_after(f, 40)] == list(range(41, 52))
    with f.open("a") as fh:
        fh.write('te"}\n\nnot json\n')
    assert csmod._records_after(f, 40) == OpLog(f, None, 40).own() and csmod._records_after(f, 40)[-1]["seq"] == 52
    f.write_text(f.read_text().replace('"i":45', '"i":-45'))              # rewritten: another file
    assert csmod._records_after(f, 40) == OpLog(f, None, 40).own()
    assert csmod._records_after(tmp_path / "none.jsonl", 0) == []


# ---------------------------------------------------------------- the baseline, and opening


def test_the_baseline_takes_no_layout_cell_nor_undo():
    root, srv, cs = tc.setup()
    db = srv.db("p", "l")
    with db.recording("new_cell", {}, "user"):
        db.new_cell("made")
    db.save()
    meta = cs.create("p", "T")
    B = meta["baseline"]
    hist = srv.project(B).path / "history" / "l.jsonl"
    before = hist.read_bytes()
    c = TestClient(create_app(srv, None))
    assert c.post(f"/api/p/{B}/l/l/layout-cells", json={"name": "intruder"}).status_code == 403
    for step in ("undo", "redo"):                                         # nothing to undo there: nothing done
        r = c.post(f"/api/p/{B}/l/l/{step}")
        assert r.status_code == 200 and r.json()["result"] is None and r.json()["undo"] == [0, 0]
    assert srv.db(B, "l").layout.cell("intruder") is None and srv.db(B, "l").layout.cell("made") is not None
    assert srv.db(B, "l").can_undo_redo() == (0, 0) and hist.read_bytes() == before
    assert tg.git(srv.project(B).path, "status", "--porcelain") == ""
    with pytest.raises(PermissionError):
        srv.oplog(B, "l").append({"op": "note"})


def test_opening_reads_no_cell_file(monkeypatch):
    root, srv, cs = tc.setup()
    libs = root / "p" / "libs"
    real = Path.read_bytes

    def read_bytes(self):
        assert not (self.is_relative_to(libs) and self.suffix in (".sch", ".sym", ".oas")), f"read {self}"
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    meta = cs.create("p", "T")
    rec = json.loads((root / "p" / "changesets" / f"{meta['id']}.json").read_text())
    assert rec["base"] == {"l": {}} and rec["libs"] == ["l"]
