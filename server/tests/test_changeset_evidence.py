"""Proposal evidence survives closure; checks match the verified inputs.

Real operation logs, job files and content hashes; synthetic verification runners
keep the tests independent of PDKs and external tools.
"""
import errno
import json
import os
import shutil
import signal
import sys
import threading
import time
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import klayout.db as kdb  # noqa: E402
from test_changeset import setup, value  # noqa: E402
from undine.store import provenance  # noqa: E402
from undine.simulation import sim
from undine.server.api import Run, Server  # noqa: E402
from undine.store.changeset import ChangesetError, Changesets  # noqa: E402


def _edit(srv, meta, resistance="2k", x=0):
    ed = srv.editor(meta["preview"], "l", "top", "schematic")
    with ed.recording("set_props", {"id": "R1"}, "agent:test", "less current", meta["id"]):
        ed.set_props("i", "R1", {"value": resistance})
    db = srv.db(meta["preview"], "l")
    li = db.layout.layer(68, 20)
    with db.recording("create", {}, "agent:test", "routing", meta["id"]):
        db._commit("top", [("ins", li, kdb.Box(x, 0, x + 100, 100))])


@pytest.fixture
def proposal(monkeypatch):
    root, srv, cs = setup()
    meta = cs.create("p", "Raise R1", "less current", "agent:test")
    _edit(srv, meta)
    case = SimpleNamespace(root=root, srv=srv, cs=cs, meta=meta, launched=[])

    def netlist_sha(project, lib, cell, *args, **kwargs):
        return provenance.file_sha(srv.project(project).library(lib).path / f"{cell}.sch")

    monkeypatch.setattr(srv, "netlist_sha", netlist_sha)

    def inputs(project, kind):
        design = [provenance.layout_entry(srv.db(project, "l"), "l", "top")]
        if kind == "lvs":
            design.append({"view": "netlist", "lib": "l", "cell": "top", "top": "top", "mode": "lvs",
                           "sha": netlist_sha(project, "l", "top")})
        return {"design": design, "pdk": None, "deck": None, "tools": {}, "options": {}}

    def job(project, kind, state="done", inp=None):
        jid = f"test-{len(case.launched):04d}-{kind}"
        path = srv.jobs.runs_dir(srv.project(project).path) / jid
        path.mkdir()
        (path / "input.cir").write_text(f"* input for {jid}\n")
        (path / "result.txt").write_text(f"result for {jid}\n")
        (path / "nested").mkdir()
        (path / "nested" / "trace.raw").write_bytes(b"\x00\x01\x02")
        j = sim.Job(path)
        summary = {"total": 0} if kind == "drc" else {"match": True}
        j.set(id=jid, kind=kind, lib="l", cell="top", tool="klayout", state=state, summary=summary,
              **provenance.meta(inp if inp is not None else inputs(project, kind)))
        j.set(**provenance.outputs(path))
        srv.jobs.live[jid] = j
        case.launched.append((project, kind, j))
        return j

    def prepare(kind, project, lib, cell, *args, **kwargs):
        inp = inputs(project, kind)
        return Run(project, kind, "klayout", lib, cell, None, inp, lambda: job(project, kind, inp=inp))

    monkeypatch.setattr(srv, "prepare_drc", lambda *a, **kw: prepare("drc", *a, **kw))
    monkeypatch.setattr(srv, "prepare_lvs", lambda *a, **kw: prepare("lvs", *a, **kw))
    case.job = job
    case.inputs = inputs
    try:
        yield case
    finally:
        shutil.rmtree(root)


def _rows(case):
    return case.cs.get("p", case.meta["id"])["verify"]


def _selected(case, side="work", kind="drc"):
    return next(row for row in _rows(case) if row["side"] == side and row["kind"] == kind)


def _job(case, row):
    return case.srv.jobs.get(case.srv.project(row["project"]).path, row["job"])


def test_required_checks_are_explicit_before_the_first_run(proposal):
    rows = _rows(proposal)
    assert {(r["side"], r["kind"]) for r in rows} == {
        (side, kind) for side in ("base", "work") for kind in ("drc", "lvs")}
    assert all(r["state"] == "not_run" and r["ok"] is None and r["fresh"] is None for r in rows)
    assert all(isinstance(r["changed"], list) for r in rows)


def test_verify_reuses_fresh_passing_and_failing_results(proposal):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    original = {(r["side"], r["kind"]): r["job"] for r in _rows(p)}
    _job(p, _selected(p, "work", "drc")).set(summary={"total": 3})
    _job(p, _selected(p, "base", "lvs")).set(summary={"match": False})
    p.cs.verify("p", p.meta["id"])
    assert len(p.launched) == 4
    assert {(r["side"], r["kind"]): r["job"] for r in _rows(p)} == original
    assert all(r["fresh"] is True and r["changed"] == [] for r in _rows(p))
    assert _selected(p, "work", "drc")["ok"] is False
    assert _selected(p, "base", "lvs")["ok"] is False


@pytest.mark.parametrize("state", ["queued", "running"])
def test_verify_does_not_duplicate_current_pending_jobs(proposal, state):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    for row in _rows(p):
        _job(p, row).set(state=state)
    p.cs.verify("p", p.meta["id"])
    assert len(p.launched) == 4
    assert all(r["state"] == state and r["ok"] is None for r in _rows(p))


def test_stale_results_keep_their_outcome_and_only_changed_side_is_rerun(proposal):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    old = {(r["side"], r["kind"]): r["job"] for r in _rows(p)}
    _edit(p.srv, p.meta, resistance="3k", x=200)
    assert _selected(p)["ok"] is True
    assert _selected(p)["fresh"] is False
    assert _selected(p)["changed"]
    assert _selected(p, "base")["fresh"] is True
    p.cs.verify("p", p.meta["id"])
    assert len(p.launched) == 6
    for row in _rows(p):
        assert row["fresh"] is True
        assert (row["job"] == old[row["side"], row["kind"]]) == (row["side"] == "base")


@pytest.mark.parametrize("problem", ["missing", "unknown", "failed", "unknown_inputs"])
def test_verify_retries_missing_unknown_and_failed_evidence(proposal, problem):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    row = _selected(p)
    job = _job(p, row)
    if problem == "missing":
        shutil.rmtree(job.path)
    elif problem == "unknown_inputs":
        status = job.status()
        status.pop("inputs")  # A matching legacy key alone is not proof of freshness.
        (job.path / "status.json").write_text(json.dumps(status))
        assert _selected(p)["fresh"] is None
    else:
        job.set(state=problem)
    p.cs.verify("p", p.meta["id"])
    assert len(p.launched) == 5
    assert _selected(p)["job"] != row["job"]
    assert _selected(p)["fresh"] is True


@pytest.mark.parametrize("action", ["apply", "discard"])
def test_closure_archives_all_runs_diff_and_frozen_results(proposal, action):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    _edit(p.srv, p.meta, resistance="3k", x=200)
    p.cs.verify("p", p.meta["id"])
    # Non-check and superseded runs are evidence too. More than 100 in each side
    # exercises the limit of Jobs.list, which archival must not rely on.
    for side in ("preview", "baseline"):
        for _ in range(101):
            p.job(p.meta[side], "sim")
    before = p.cs.get("p", p.meta["id"])
    originals = {j.id: (project, j.status(), (j.path / "nested" / "trace.raw").read_bytes())
                 for project, _, j in p.launched}
    getattr(p.cs, action)("p", p.meta["id"])
    closed = p.cs.get("p", p.meta["id"])
    assert closed["changes"] == before["changes"]
    assert closed["verify"] == before["verify"]
    evidence = p.srv.project("p").path / closed["evidence"]["path"]
    assert evidence == p.srv.project("p").path / "changesets" / p.meta["id"] / "evidence"
    manifest = json.loads((evidence / "manifest.json").read_text())
    # version 2: a changeset on git; its commits stand for the design and its log (history/ is no longer copied)
    assert manifest["version"] == 2 and manifest["changeset"] == p.meta["id"]
    assert manifest["commits"] == {"base": closed["git"]["base"], "tip": closed["git"]["tip"]} and closed["git"]["tip"]
    assert not any(name.split("/")[1] == "history" for name in manifest["files"])
    assert manifest["changes"] == before["changes"] and manifest["verify"] == before["verify"]
    assert len(manifest["jobs"]) == len(originals) == closed["evidence"]["jobs"]
    for entry in manifest["jobs"]:
        project, status, data = originals[entry["id"]]
        side = "base" if project == p.meta["baseline"] else "work"
        assert entry["side"] == side and entry["project"] == project
        assert entry["path"] == f"{side}/runs/{entry['id']}"
        archived = evidence / entry["path"]
        assert json.loads((archived / "status.json").read_text()) == status
        assert (archived / "nested" / "trace.raw").read_bytes() == data
        for name, digest in status["files"].items():
            assert provenance.file_sha(archived / name) == digest
        assert manifest["files"][f"{entry['path']}/status.json"] == provenance.file_sha(archived / "status.json")
    for relative, digest in manifest["files"].items():
        assert provenance.file_sha(evidence / relative) == digest
    for side in ("base", "work"):
        assert (evidence / side / "project.json").is_file()
    # Review must no longer depend on either shadow project or cached editors.
    shutil.rmtree(p.root / ".changesets" / "p" / p.meta["id"], ignore_errors=True)
    restarted = Changesets(Server(p.root, {}, [p.root]))
    recovered = restarted.get("p", p.meta["id"])
    assert recovered["changes"] == before["changes"] and recovered["verify"] == before["verify"]
    if action == "apply":
        restarted.revert("p", p.meta["id"])
        reverted = restarted.get("p", p.meta["id"])
        assert reverted["status"] == "reverted"
        assert reverted["changes"] == before["changes"] and reverted["verify"] == before["verify"]


@pytest.mark.parametrize("action", ["apply", "discard"])
@pytest.mark.parametrize("side", ["base", "work"])
@pytest.mark.parametrize("state", ["queued", "running"])
def test_active_unlisted_runs_block_closure_without_losing_work(proposal, action, side, state):
    p = proposal
    project = p.meta["baseline"] if side == "base" else p.meta["preview"]
    busy = p.job(project, "sim", state=state)
    for _ in range(101):
        p.job(project, "sim")  # Busy run falls beyond Jobs.list's newest 100.
    with pytest.raises(ChangesetError):
        getattr(p.cs, action)("p", p.meta["id"])
    assert p.cs.get("p", p.meta["id"])["status"] == "open"
    assert value(p.srv) == "1k" and value(p.srv, p.meta["preview"]) == "2k"
    assert busy.path.is_dir() and busy.status()["state"] == state
    assert p.srv.project(p.meta["baseline"]).path.is_dir()


@pytest.mark.parametrize("action", ["apply", "discard"])
def test_evidence_copy_failure_preserves_open_proposal_and_original_design(proposal, monkeypatch, action):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    before = p.cs.get("p", p.meta["id"])

    def fail_link(*args, **kwargs):
        raise OSError(28, "archive device is full")             # ENOSPC: no copy instead of a link either

    monkeypatch.setattr("undine.store.changeset.os.link", fail_link)
    with pytest.raises((ChangesetError, OSError), match="archive device is full"):
        getattr(p.cs, action)("p", p.meta["id"])
    current = p.cs.get("p", p.meta["id"])
    assert current["status"] == "open" and current["changes"] == before["changes"]
    assert value(p.srv) == "1k" and value(p.srv, p.meta["preview"]) == "2k"
    assert all(job.path.is_dir() for _, _, job in p.launched)
    assert not (p.srv.project("p").path / "changesets" / p.meta["id"] / "evidence" / "manifest.json").exists()



def test_failed_check_launch_remains_a_typed_retryable_row(proposal, monkeypatch):
    p = proposal
    original_prepare = p.srv.prepare_lvs

    def fail_prepare(*args, **kwargs):
        raise RuntimeError("test LVS deck unavailable")

    monkeypatch.setattr(p.srv, "prepare_lvs", fail_prepare)
    p.cs.verify("p", p.meta["id"])
    failures = [row for row in _rows(p) if row["kind"] == "lvs"]
    assert len(failures) == 2
    assert all(row["state"] == "failed" and row["ok"] is None and row["fresh"] is None for row in failures)
    assert all("test LVS deck unavailable" in row["error"] and not row.get("job") for row in failures)
    assert len(p.launched) == 2
    monkeypatch.setattr(p.srv, "prepare_lvs", original_prepare)
    p.cs.verify("p", p.meta["id"])
    assert len(p.launched) == 4
    assert all(row["state"] == "done" and row["fresh"] is True for row in _rows(p))


def test_old_applied_changeset_without_archive_still_opens(proposal):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    p.cs.apply("p", p.meta["id"])
    saved = p.cs._load("p", p.meta["id"])
    evidence = p.srv.project("p").path / saved.pop("evidence")["path"]
    p.cs._save("p", saved)
    shutil.rmtree(evidence)
    restarted = Changesets(Server(p.root, {}, [p.root]))
    legacy = restarted.get("p", p.meta["id"])
    assert legacy["status"] == "applied"
    assert any(op["op"] == "set_props" for change in legacy["changes"] for op in change["ops"])
    assert isinstance(legacy["verify"], list)




@pytest.mark.parametrize("action", ["apply", "discard"])
def test_closure_freezes_outdated_results_without_claiming_current(proposal, action):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    _edit(p.srv, p.meta, resistance="3k", x=200)
    before = _selected(p)
    assert before["fresh"] is False and before["ok"] is True
    getattr(p.cs, action)("p", p.meta["id"])
    after = _selected(p)
    assert after == before


def test_restarted_pending_job_is_interrupted_and_can_be_archived(proposal):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    row = _selected(p)
    job = _job(p, row)
    job.set(state="running")
    restarted = Changesets(Server(p.root, {}, [p.root]))
    current = restarted.get("p", p.meta["id"])
    interrupted = next(r for r in current["verify"] if r.get("job") == row["job"])
    assert interrupted["state"] == "interrupted" and interrupted["ok"] is None
    assert interrupted["fresh"] is True
    assert "interrupted" in interrupted["error"]
    assert job.status()["state"] == "running"  # Retain the raw evidence of interruption.
    restarted.apply("p", p.meta["id"])
    closed = restarted.get("p", p.meta["id"])
    archived = next(r for r in closed["verify"] if r.get("job") == row["job"])
    assert archived == interrupted
    path = p.srv.project("p").path / closed["evidence"]["path"] / "work" / "runs" / row["job"]
    assert json.loads((path / "status.json").read_text())["state"] == "running"


def test_verify_retries_interrupted_job_instead_of_reusing_its_key(proposal):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    row = _selected(p)
    _job(p, row).set(state="queued")
    del p.srv.jobs.live[row["job"]]
    assert _selected(p)["state"] == "interrupted"
    p.cs.verify("p", p.meta["id"])
    assert len(p.launched) == 5
    assert _selected(p)["job"] != row["job"]
    assert _selected(p)["state"] == "done"


@pytest.mark.parametrize("action", ["apply", "discard"])
@pytest.mark.parametrize("state", ["done", "failed", "cancelled"])
def test_live_process_blocks_closure_even_when_status_is_terminal(proposal, action, state):
    p = proposal
    job = p.job(p.meta["preview"], "sim", state=state)
    job.proc = SimpleNamespace(poll=lambda: None)
    with pytest.raises(ChangesetError):
        getattr(p.cs, action)("p", p.meta["id"])
    assert p.cs.get("p", p.meta["id"])["status"] == "open"
    assert value(p.srv) == "1k" and job.path.is_dir()


@pytest.mark.parametrize("action", ["apply", "discard"])
@pytest.mark.parametrize("mutation", ["edit", "new_job"])
def test_changes_during_archive_prevent_closure_without_losing_work(proposal, monkeypatch, action, mutation):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    original_link = os.link
    changed = False

    def race_link(*args, **kwargs):
        nonlocal changed
        first = not changed
        changed = True
        result = original_link(*args, **kwargs)
        if first:
            if mutation == "edit":
                _edit(p.srv, p.meta, resistance="4k", x=400)
            else:
                p.job(p.meta["preview"], "sim", state="running")
        return result

    monkeypatch.setattr("undine.store.changeset.os.link", race_link)
    with pytest.raises(ChangesetError):
        getattr(p.cs, action)("p", p.meta["id"])
    assert changed
    assert p.cs.get("p", p.meta["id"])["status"] == "open"
    assert value(p.srv) == "1k"
    assert value(p.srv, p.meta["preview"]) == ("4k" if mutation == "edit" else "2k")
    assert all(job.path.is_dir() for _, _, job in p.launched)



def test_edit_arriving_during_the_merge_preserves_work_and_applies_only_reviewed_changes(proposal, monkeypatch):
    # an apply merges the tip archived as reviewed (there is no replay any more): an edit made while it writes the
    # merged files is committed onto the branch at closure, and the working copy is kept
    from undine.vcs import branches
    p = proposal
    p.cs.verify("p", p.meta["id"])
    before = p.cs.get("p", p.meta["id"])
    original_write = branches.write_plan

    def edit_during_write(*args, **kwargs):
        result = original_write(*args, **kwargs)
        _edit(p.srv, p.meta, resistance="3k", x=200)
        return result

    monkeypatch.setattr(branches, "write_plan", edit_during_write)
    p.cs.apply("p", p.meta["id"])
    closed = p.cs.get("p", p.meta["id"])
    assert closed["status"] == "applied"
    assert closed["cleanup_warning"]
    assert closed["changes"] == before["changes"]
    assert closed["verify"] == before["verify"]
    assert value(p.srv) == "2k"
    assert value(p.srv, p.meta["preview"]) == "3k"
    assert all(job.path.is_dir() for _, _, job in p.launched)
    manifest_path = p.srv.project("p").path / closed["evidence"]["path"] / "manifest.json"
    assert json.loads(manifest_path.read_text())["changes"] == before["changes"]
    tip = json.loads(manifest_path.read_text())["commits"]["tip"]
    assert closed["applied"]["tip"] == tip and closed["git"]["tip"] != tip and closed["git"]["ref"] is None
    assert branches.resolve(p.srv.project("p").path, f"{closed['git']['tip']}~1") == tip     # the late edit, kept



# ---------------------------------------------------------------- the runs as hard links, each file read once at most


def _manifest(case, cid):
    return json.loads((case.srv.project("p").path / "changesets" / cid / "evidence" / "manifest.json").read_text())


def _big_run(case, project, size=1 << 20):
    """A finished run with a waveform of `size` bytes, its outputs hashed as a job's are when it ends."""
    j = case.job(project, "sim")
    (j.path / "out.raw").write_bytes(os.urandom(size))
    j.set(**provenance.outputs(j.path))
    return j


@pytest.mark.parametrize("action", ["apply", "discard"])
def test_runs_are_archived_as_hard_links_and_left_the_only_name_of_their_files(proposal, action):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    j = _big_run(p, p.meta["preview"])
    before = (j.path / "out.raw").stat()
    getattr(p.cs, action)("p", p.meta["id"])
    evidence = p.srv.project("p").path / p.cs.get("p", p.meta["id"])["evidence"]["path"]
    archived = (evidence / "work" / "runs" / j.id / "out.raw").stat()
    assert (archived.st_ino, archived.st_size, archived.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert archived.st_nlink == 1                       # the working copy went with the closure: the evidence holds it
    manifest = _manifest(p, p.meta["id"])
    runs = [rel for rel in manifest["files"] if rel.split("/")[1] == "runs"]
    assert manifest["linked"] == len(runs) and len(runs) < len(manifest["files"])   # project.json is a copy
    assert manifest["sources"][f"work/runs/{j.id}/out.raw"] == provenance.stamp(before)
    assert manifest["bytes"] == sum((evidence / rel).stat().st_size for rel in manifest["files"])
    closed = p.cs._load("p", p.meta["id"])["evidence"]
    assert closed["bytes"] == manifest["bytes"] and closed["jobs"] == len(manifest["jobs"])


def test_runs_are_copied_where_no_link_can_be_made(proposal, monkeypatch):
    from undine.store import changeset as csmod
    p = proposal
    p.cs.verify("p", p.meta["id"])
    _big_run(p, p.meta["preview"], 4096)
    tried, read, seen = [], [], {}

    def other_file_system(src, dst, *args, **kwargs):
        tried.append(src)
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr("undine.store.changeset.os.link", other_file_system)
    sha, archive = csmod._sha, Changesets._archive
    monkeypatch.setattr(csmod, "_sha", lambda path, progress: read.append(path) or sha(path, progress))

    def archived(self, project, meta):
        out = archive(self, project, meta)
        seen.update(self.srv.closings.public(meta["id"]))
        return out

    monkeypatch.setattr(Changesets, "_archive", archived)
    p.cs.apply("p", p.meta["id"])
    assert len(tried) == 1                              # another file system: no file of it is tried again
    manifest = _manifest(p, p.meta["id"])
    evidence = p.srv.project("p").path / "changesets" / p.meta["id"] / "evidence"
    assert manifest["linked"] == 0 and p.cs.get("p", p.meta["id"])["status"] == "applied"
    for rel, digest in manifest["files"].items():
        assert provenance.file_sha(evidence / rel) == digest
    assert read == []                                   # hashed while copied, not read again
    assert seen["bytes"] == seen["bytes_total"] == manifest["bytes"] and seen["files"] == seen["files_total"]


# ---------------------------------------------------------------- what may still write a run: stopped, copied


def _orphaned(log: Path):
    """A job's process that has ended, leaving what it started writing into the run's log through the stdout it was
    given, as a cancelled build's make and g++ outlive verilator: a sim.Process (its own process group)."""
    out = open(log, "ab")
    try:
        proc = sim.Process(["sh", "-c", "(while :; do echo more; sleep 0.02; done) & exit 0"], cwd=log.parent,
                           stdout=out, stderr=out)
    finally:
        out.close()
    proc.wait(10)
    return proc


def test_stopping_a_job_stops_what_its_process_started(tmp_path):
    log = tmp_path / "log.txt"
    log.write_bytes(b"")
    proc = _orphaned(log)
    assert proc.poll() == 0 and sim.alive(proc)         # the process is gone, what it started is not
    size = log.stat().st_size
    for _ in range(100):
        if log.stat().st_size > size:
            break
        threading.Event().wait(0.02)
    assert log.stat().st_size > size                    # and it writes on
    t0 = time.monotonic()
    sim.stop(proc)
    assert not sim.alive(proc) and time.monotonic() - t0 < sim.STOP_GRACE_S
    size = log.stat().st_size
    threading.Event().wait(0.2)
    assert log.stat().st_size == size                   # nothing writes any more


def test_a_group_seen_empty_is_never_signalled_again(tmp_path, monkeypatch):
    """A finished job stays in Jobs.live (a check's does): at the server's exit, or a cancel, its group's id may be a
    terminal's session by then (pid_max can be 99999), which must not be killed."""
    proc = sim.Process(["true"], cwd=tmp_path)
    proc.wait(10)
    assert proc.ended and proc.group is None and not sim.alive(proc)
    lingering = _orphaned(tmp_path / "log.txt")         # ended, what it started not: still signalled, then ended
    assert not lingering.ended and lingering.group == lingering.pid
    sim.stop(lingering)
    assert lingering.ended
    signalled = []
    monkeypatch.setattr(sim.os, "killpg", lambda group, sig: signalled.append((group, sig)))
    monkeypatch.setattr(sim, "_ALL", weakref.WeakSet())     # only these jobs at the "exit"
    jobs = sim.Jobs()
    for i, p in enumerate((proc, lingering)):
        job = sim.Job(tmp_path / f"j{i}")
        job.proc = p
        jobs.live[job.id] = job
    sim._stop_all()
    sim._signal(proc, signal.SIGKILL)
    assert signalled == []


def test_stopping_an_adopted_process_stops_its_group_and_only_its_own(tmp_path):
    """A server's job process leads a group of its own (sim.Process); the next server adopts it as a sim.Foreign, and
    a stop (cancel, time limit, --stop-jobs) ends what it started too. One that leads no group (an Undine before groups
    started it in its own) is signalled alone, never the group it is in; a group whose id is a process's with another
    start time (its pid given again: the group had gone) has no members of its."""
    import subprocess
    log = tmp_path / "log.txt"
    log.write_bytes(b"")
    out = open(log, "ab")
    try:
        proc = sim.Process(["sh", "-c", "(while :; do echo more; sleep 0.02; done) & sleep 60"], cwd=tmp_path,
                           stdout=out, stderr=out)
        plain = subprocess.Popen(["sleep", "60"], cwd=tmp_path)            # in this process's group
    finally:
        out.close()
    try:
        adopted = sim.Foreign(proc.pid, sim._proc_stat(proc.pid)[2])
        alone = sim.Foreign(plain.pid, sim._proc_stat(plain.pid)[2])
        assert adopted.group == proc.pid and sim.alive(adopted)
        assert alone.group is None and sim.alive(alone)
        sim.stop(adopted)
        assert not sim.alive(adopted) and adopted.ended and adopted.group is None
        size = log.stat().st_size
        threading.Event().wait(0.2)
        assert log.stat().st_size == size                   # what it started is gone too
        sim.stop(alone)
        assert not sim.alive(alone) and plain.wait(5) == -signal.SIGTERM
        assert not sim._members(os.getpid(), leader=-1)     # this pid's start time is not -1
    finally:
        for p in (proc, plain):
            if p.poll() is None:
                p.kill()
            p.wait()


def test_a_job_adopted_across_a_restart_blocks_the_closure_until_it_ends_then_is_archived(proposal):
    """A changeset's simulation still runs when the server restarts: the next server adopts it; the changeset cannot
    be applied while it runs, and once it ends (recorded by the adopting server, its outputs' hashes and stamps) its run
    is archived as hard links with the hashes it recorded."""
    from undine import tools
    if not tools.find("ngspice"):
        pytest.skip("ngspice not installed")
    p = proposal
    cid = p.meta["id"]
    p.cs.verify("p", cid)
    lines = ["* rc ladder", "V1 n0 0 PULSE(0 1 0 1n 1n 50n 100n)"]
    for i in range(40):
        lines += [f"R{i} n{i} n{i + 1} 1k", f"C{i} n{i + 1} 0 10f"]
    lines += [".tran 0.05n 30u", ".save v(n40)", ".meas tran vavg avg v(n40) from=15u to=30u", ".end"]
    work = p.srv.project(p.meta["preview"]).path
    job = p.srv.jobs.submit(work, "ladder", "\n".join(lines) + "\n", None, meta={"kind": "sim"})
    t0 = time.time()
    while "start" not in (job.status().get("process") or {}) and time.time() - t0 < 30:
        time.sleep(0.05)
    first = p.srv.jobs
    first.shutdown()                                        # the restart: its ngspice runs on
    p.srv.jobs = sim.Jobs(root=p.srv.store.root)            # the next server
    try:
        assert p.srv.jobs.resumed["adopted"] == [job.id] and isinstance(p.srv.jobs.live[job.id].proc, sim.Foreign)
        with pytest.raises(ChangesetError, match="running jobs"):
            p.cs.apply("p", cid)
        assert p.cs.get("p", cid)["status"] == "open"
        t0 = time.time()
        while job.status()["state"] == "running" and time.time() - t0 < 300:
            time.sleep(0.2)
        st = job.status()
        assert st["state"] == "done" and st["adopted"] and set(st["stamps"]) == set(st["files"]), st
        p.cs.apply("p", cid)
        evidence = p.srv.project("p").path / "changesets" / cid / "evidence"
        manifest = _manifest(p, cid)
        run = f"work/runs/{job.id}"
        archived = {rel: sha for rel, sha in manifest["files"].items() if rel.startswith(run + "/")}
        assert {f"{run}/out.raw", f"{run}/log.txt"} <= set(archived)
        for name, sha in st["files"].items():
            assert archived[f"{run}/{name}"] == sha == provenance.file_sha(evidence / run / name)
        for name in ("out.raw", "log.txt"):                 # linked: the inode the job recorded (a copy has another)
            assert os.stat(evidence / run / name).st_ino == st["stamps"][name][2], name
        assert manifest["linked"] > 0 and p.cs.evidence("p", cid, check=True)["check"]["changed"] == []
    finally:
        p.srv.jobs.shutdown(stop_jobs=True)


def test_what_a_jobs_process_started_blocks_the_closure_until_it_is_stopped(proposal):
    p = proposal
    cid = p.meta["id"]
    p.cs.verify("p", cid)
    j = _big_run(p, p.meta["preview"], 4096)
    j.set(state="cancelled")                            # the cancel: its process ended, its compiler not yet
    j.proc = _orphaned(j.path / "log.txt")
    try:
        with pytest.raises(ChangesetError, match="running jobs"):
            p.cs.apply("p", cid)
        assert p.cs.get("p", cid)["status"] == "open"
    finally:
        sim.stop(j.proc)
    p.cs.apply("p", cid)
    archived = p.srv.project("p").path / "changesets" / cid / "evidence" / "work" / "runs" / j.id / "log.txt"
    assert archived.stat().st_nlink == 1 and p.cs.evidence("p", cid, check=True)["check"]["changed"] == []


def test_what_a_job_left_running_after_it_ended_blocks_the_closure_until_cancelled(proposal):
    """A job's process recorded (sim.Process with its job) ended by itself, but something it started runs on: the
    closure waits for it also once the job has left the server's live jobs, and cancel_job stops it."""
    p = proposal
    cid = p.meta["id"]
    p.cs.verify("p", cid)
    j = _big_run(p, p.meta["preview"], 4096)
    proc = sim.Process(["sh", "-c", "sleep 60 & exit 0"], job=j, cwd="/", stdout=sim.subprocess.DEVNULL)
    try:
        assert j.proc is proc and j.status()["process"]["pid"] == proc.pid
        proc.wait(10)
        p.srv.jobs.live.pop(j.id)                           # ended, as a simulation leaves them
        assert sim.left(j.status()) is not None
        with pytest.raises(ChangesetError, match="processes it started still run"):
            p.cs.apply("p", cid)
        from undine.agents import agent_tools as T
        me = T.ToolCtx(p.srv, "p", actor="agent:test", changeset=cid)
        out = json.loads(T.call(me, "cancel_job", {"job": j.id})[0])
        assert out["state"] == "done" and "being stopped" in out["note"], out
        t0 = time.time()
        while sim.left(j.status()) is not None and time.time() - t0 < 10:
            time.sleep(0.05)
        assert sim.left(j.status()) is None
        p.cs.apply("p", cid)
    finally:
        sim.stop(proc)


@pytest.mark.parametrize("log", ["Circuit: x\nDoing analysis at TEMP = 27\n",
                                 "Circuit: x\nError: unknown parameter (pd) in a model note\nDoing analysis at TEMP = 27\n"])
def test_an_adopted_ngspice_killed_while_no_server_watched_is_cut_whatever_its_log_says(tmp_path, log):
    """Its log not finished (no ENDED line) and its exit code unknown: "cut", with an error line of the log kept apart,
    so that its run is copied (what it started may outlive it), not linked."""
    from undine.store import changeset
    d = tmp_path / "runs" / "j1"
    d.mkdir(parents=True)
    (d / "log.txt").write_text(log)
    job = sim.Job(d)
    job.set(id="j1", state="running", created=time.time())
    job.proc = sim.Foreign(None, None)                      # gone
    sim.Jobs()._follow(job, 100, time.time())
    st = job.status()
    assert st["state"] == "failed" and st["cut"] and st["error"] == sim.CUT
    assert st.get("log_error") == ("Error: unknown parameter (pd) in a model note" if "Error" in log else None)
    assert not changeset._ended_by_itself(st)


@pytest.mark.parametrize("why", ["cancelled", "timed_out", "open_for_writing", "working_in"])
def test_a_run_that_may_still_be_written_is_copied_and_a_late_write_leaves_the_evidence(proposal, monkeypatch, why):
    import subprocess
    p = proposal
    if why in ("cancelled", "timed_out"):               # its status alone says so
        monkeypatch.setattr(sim, "writers", lambda runs: set())
    cid = p.meta["id"]
    p.cs.verify("p", cid)
    j = _big_run(p, p.meta["preview"], 4096)
    quiet = _big_run(p, p.meta["preview"], 4096)
    log = j.path / "log.txt"
    log.write_text("build started\n")
    j.set(**provenance.outputs(j.path))
    if why == "cancelled":
        j.set(state="cancelled")
    elif why == "timed_out":
        j.set(state="failed", error="timed out", timed_out=True)
    writer = open(log, "a")                             # what outlived the job: its stdout, the run its folder
    shell = subprocess.Popen(["sleep", "60"], cwd=j.path) if why == "working_in" else None
    try:
        if why == "working_in":
            writer.close()
        p.cs.apply("p", cid)
        evidence = p.srv.project("p").path / "changesets" / cid / "evidence" / "work" / "runs"
        assert (evidence / quiet.id / "out.raw").stat().st_nlink == 1          # linked: the working copy is gone
        assert (evidence / j.id / "out.raw").stat().st_nlink == 1 and not j.path.exists()
        manifest = _manifest(p, cid)
        assert manifest["linked"] == len([r for r in manifest["files"] if "/runs/" in r and f"/{j.id}/" not in r])
        if not writer.closed:
            writer.write("g++ ... more output after the closure\n")
            writer.flush()
        assert (evidence / j.id / "log.txt").read_text() == "build started\n"
        assert p.cs.evidence("p", cid, check=True)["check"]["changed"] == []
    finally:
        writer.close()
        if shell:
            shell.kill()
            shell.wait()


def test_the_runs_a_process_may_write_are_seen_whichever_process(tmp_path):
    import subprocess
    runs = tmp_path / "runs"
    for name in ("a", "b", "c", "d"):
        (runs / name).mkdir(parents=True)
        (runs / name / "log.txt").write_text("x\n")
    with open(runs / "a" / "log.txt", "a"), open(runs / "b" / "log.txt"):     # for writing; only for reading
        shell = subprocess.Popen(["sleep", "60"], cwd=runs / "c")
        try:
            assert sim.writers([runs, tmp_path / "none"]) == {Path(os.path.realpath(runs)) / n for n in ("a", "c")}
        finally:
            shell.kill()
            shell.wait()
    assert sim.writers([runs]) == set()


def test_each_file_is_read_once_at_most_and_a_finished_runs_outputs_not_at_all(proposal, monkeypatch):
    from undine.store import changeset as csmod
    p = proposal
    p.cs.verify("p", p.meta["id"])
    j = _big_run(p, p.meta["preview"])
    recorded = j.status()["files"]["out.raw"]
    late = _big_run(p, p.meta["baseline"], 4096)
    (late.path / "out.raw").write_bytes(b"written after the run ended")    # its stamp is not the one recorded
    read, sha = [], csmod._sha
    monkeypatch.setattr(csmod, "_sha", lambda path, progress: read.append(path) or sha(path, progress))
    hashed_again = []
    file_sha = provenance.file_sha
    monkeypatch.setattr(provenance, "file_sha", lambda path: hashed_again.append(path) or file_sha(path))
    p.cs.apply("p", p.meta["id"])
    rels = [r.as_posix().split("/evidence/", 1)[1] for r in read]
    assert len(rels) == len(set(rels))                  # once each
    assert f"work/runs/{j.id}/out.raw" not in rels and f"work/runs/{j.id}/nested/trace.raw" not in rels
    assert f"base/runs/{late.id}/out.raw" in rels       # changed since its run hashed it: read
    assert {rel.rsplit("/", 1)[-1] for rel in rels} <= {"status.json", "project.json", "undine.lock", "out.raw"}
    assert not [x for x in hashed_again if "runs" in Path(x).parts]       # no source read for the staleness checks
    manifest = _manifest(p, p.meta["id"])
    assert manifest["files"][f"base/runs/{late.id}/out.raw"] == provenance.text_sha("written after the run ended")
    assert manifest["files"][f"work/runs/{j.id}/out.raw"] == recorded


def test_a_status_renamed_in_while_the_evidence_is_saved_stops_the_closure(proposal, monkeypatch):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    j = _big_run(p, p.meta["preview"], 4096)
    link = os.link

    def then_set(src, dst, *args, **kwargs):
        out = link(src, dst, *args, **kwargs)
        if Path(src) == j.path / "status.json":
            j.set(note="written after its link was made")                # renamed in: another inode
        return out

    monkeypatch.setattr("undine.store.changeset.os.link", then_set)
    with pytest.raises(ChangesetError, match="changed while saving evidence"):
        p.cs.apply("p", p.meta["id"])
    assert p.cs.get("p", p.meta["id"])["status"] == "open" and value(p.srv) == "1k"
    assert not (p.srv.project("p").path / "changesets" / p.meta["id"] / "evidence").exists()


def test_a_manifest_from_before_stamps_is_compared_by_its_hashes_at_the_cleanup(proposal, monkeypatch):
    p = proposal
    p.cs.verify("p", p.meta["id"])
    cleanup = Changesets._cleanup

    def older(self, project, meta, manifest, discard=False):
        return cleanup(self, project, meta, {k: v for k, v in manifest.items() if k not in ("sources", "linked", "bytes")},
                       discard)

    monkeypatch.setattr(Changesets, "_cleanup", older)
    p.cs.apply("p", p.meta["id"])
    closed = p.cs.get("p", p.meta["id"])
    assert closed["status"] == "applied" and not closed.get("cleanup_warning") and closed["retained"] is False


def test_evidence_a_killed_server_left_staged_is_swept_and_counted_nowhere(proposal):
    from undine.store import changeset as csmod
    p = proposal
    cid = p.meta["id"]
    p.cs.verify("p", cid)
    _big_run(p, p.meta["preview"], 4096)
    p.cs.apply("p", cid)
    parent = p.srv.project("p").path / "changesets" / cid
    left = parent / ".evidence-k1ll3d" / "evidence" / "work" / "runs" / "x"
    left.mkdir(parents=True)
    (left / "out.raw").write_bytes(b"\x00" * 8192)
    busy = p.srv.project("p").path / "changesets" / "cs-0b5e55" / ".evidence-s4v1ng" / "evidence"
    busy.mkdir(parents=True)                            # one a closure is saving now: left alone
    restarted = Changesets(Server(p.root, {}, [p.root]))
    with csmod._busy(restarted._shadow_root("p", "cs-0b5e55")):
        sizes = restarted.evidence_sizes("p")
        assert busy.is_dir()
    assert not (parent / ".evidence-k1ll3d").exists() and (parent / "evidence" / "manifest.json").is_file()
    assert sizes["bytes"] == sum(f.stat().st_size for f in (parent / "evidence").rglob("*") if f.is_file())


def test_a_kept_working_copy_holds_the_evidences_files_and_a_prune_says_what_it_frees(proposal, monkeypatch):
    p = proposal
    cid = p.meta["id"]
    p.cs.verify("p", cid)
    j = _big_run(p, p.meta["preview"], 8192)
    cleanup = Changesets._cleanup

    def late(self, project, meta, manifest, discard=False):
        _edit(self.srv, meta, resistance="9k", x=900)   # a late edit: the working copy is kept
        return cleanup(self, project, meta, manifest, discard)

    monkeypatch.setattr(Changesets, "_cleanup", late)
    p.cs.apply("p", cid)
    closed = p.cs.get("p", cid)
    assert closed["retained"] and (j.path / "out.raw").stat().st_nlink == 2
    size = closed["evidence"]["size"]
    assert size["raw_shared"] == size["raw"] >= 8192 and size["shared"] >= size["raw"]
    out = p.cs.prune_evidence("p", cid, "kadomoto")["pruned"]
    assert out["bytes"] == size["raw"] and out["freed"] == 0          # the working copy still holds them
    assert (j.path / "out.raw").stat().st_nlink == 1


def test_a_jobs_status_and_log_are_renamed_in_never_written_in_place(tmp_path, monkeypatch):
    j = sim.Job(tmp_path)
    j.set(state="done", models=[{"library": "/pdk/sky130.lib.spice", "section": "tt", "full": "test"}])
    os.link(tmp_path / "status.json", tmp_path / "kept.json")            # as the evidence holds it
    j.set(state="failed")
    assert json.loads((tmp_path / "kept.json").read_text())["state"] == "done" and j.status()["state"] == "failed"
    (tmp_path / "log.txt").write_text("ngspice\n")
    os.link(tmp_path / "log.txt", tmp_path / "kept.txt")
    sim._log_models(j)
    assert (tmp_path / "kept.txt").read_text() == "ngspice\n"
    assert (tmp_path / "log.txt").read_text().startswith("Undine: models: sky130.lib.spice section tt read whole")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["kept.json", "kept.txt", "log.txt", "status.json"]


def test_a_runs_outputs_record_the_stamps_they_were_hashed_with(tmp_path):
    (tmp_path / "out.raw").write_bytes(b"\x00" * 10)
    (tmp_path / "status.json").write_text("{}")
    out = provenance.outputs(tmp_path)
    assert set(out["files"]) == set(out["stamps"]) == {"out.raw"}
    assert out["stamps"]["out.raw"] == provenance.stamp((tmp_path / "out.raw").stat())


# ---------------------------------------------------------------- how far a closure is, and one waiting for another


def test_a_closure_says_how_far_it_is_a_second_waits_and_the_same_again_is_refused(proposal, monkeypatch):
    import threading
    from fastapi.testclient import TestClient
    from undine.server.api import create_app
    from undine.store.changeset import Busy
    p = proposal
    cid = p.meta["id"]
    other = p.cs.create("p", "Other", "r", "agent:test")
    p.cs.verify("p", cid)
    _big_run(p, p.meta["preview"])
    client = TestClient(create_app(p.srv, None))
    gate, merging, seen = threading.Event(), threading.Event(), {}
    archive, merge = Changesets._archive, Changesets._merge

    def archived(self, project, meta):
        out = archive(self, project, meta)
        seen["evidence"] = self.srv.closings.public(meta["id"])
        return out

    def held(self, project, meta, action, *args, **kwargs):
        if meta["id"] == cid:
            merging.set()
            gate.wait(60)
        return merge(self, project, meta, action, *args, **kwargs)

    monkeypatch.setattr(Changesets, "_archive", archived)
    monkeypatch.setattr(Changesets, "_merge", held)
    done = []
    first = threading.Thread(target=lambda: done.append(p.cs.apply("p", cid, "kadomoto")))
    first.start()
    assert merging.wait(60)
    ev = seen["evidence"]
    assert ev["phase"] == "evidence" and ev["action"] == "apply" and ev["by"] == "kadomoto"
    assert ev["files"] == ev["files_total"] > 0 and ev["bytes"] == ev["bytes_total"] >= 1 << 20
    assert p.cs.get("p", cid)["closing"]["phase"] == "merging"
    with pytest.raises(Busy, match=r"being applied already; wait for it to finish$"):
        p.cs.apply("p", cid)
    r = client.post(f"/api/p/p/changesets/{cid}/discard")
    assert r.status_code == 409 and r.json()["code"] == "busy"
    second = threading.Thread(target=lambda: done.append(p.cs.discard("p", other["id"])))
    second.start()
    for _ in range(600):
        if p.srv.closings.get(other["id"]) is not None:
            break
        threading.Event().wait(0.05)
    waiting = p.cs.get("p", other["id"])["closing"]
    assert waiting["phase"] == "waiting" and waiting["action"] == "discard"
    assert waiting["behind"] == {"id": cid, "title": "Raise R1", "action": "apply"}
    live = {x["id"]: x for x in client.get("/api/p/p/live").json()}
    assert live[cid]["writer"] is None and live[cid]["closing"]["phase"] == "merging"
    assert live[other["id"]]["closing"]["phase"] == "waiting"
    assert {r["id"]: r.get("closing", {}).get("phase") for r in p.cs.list("p")} == {cid: "merging", other["id"]: "waiting"}
    gate.set()
    first.join(60)
    second.join(60)
    assert [d["status"] for d in done] == ["applied", "discarded"]
    assert client.get("/api/p/p/live").json() == [] and "closing" not in p.cs.get("p", cid)


# ---------------------------------------------------------------- its size, and a person pruning its waveforms


def test_a_person_prunes_the_waveforms_of_a_closed_changeset_and_its_hashes_stay(proposal):
    from fastapi.testclient import TestClient
    from undine.server.api import create_app
    p = proposal
    cid = p.meta["id"]
    client = TestClient(create_app(p.srv, None))
    p.cs.verify("p", cid)
    j = _big_run(p, p.meta["preview"], 8192)
    assert client.post(f"/api/p/p/changesets/{cid}/evidence/prune").status_code == 400       # open: nothing archived yet
    p.cs.apply("p", cid)
    evidence = p.srv.project("p").path / "changesets" / cid / "evidence"
    raws = sorted(rel for rel in _manifest(p, cid)["files"] if rel.endswith(".raw"))
    size = p.cs.get("p", cid)["evidence"]["size"]
    assert size["raw"] == sum((evidence / rel).stat().st_size for rel in raws) >= 8192
    assert size["bytes"] == sum(f.stat().st_size for f in evidence.rglob("*") if f.is_file())
    sizes = client.get("/api/p/p/changesets/evidence").json()
    assert sizes["changesets"][cid] == {**size, "pruned": None} and (sizes["bytes"], sizes["raw"]) == (size["bytes"], size["raw"])
    r = client.post(f"/api/p/p/changesets/{cid}/evidence/prune", headers={"X-Undine-Actor": "agent:test"})
    assert r.status_code == 403 and all((evidence / rel).is_file() for rel in raws)              # an agent's: refused
    r = client.post(f"/api/p/p/changesets/{cid}/evidence/prune", headers={"X-Undine-Actor": "kadomoto"})
    assert r.status_code == 200
    out = r.json()
    assert out["pruned"]["files"] == len(raws) and out["pruned"]["bytes"] == size["raw"] and out["pruned"]["by"] == "kadomoto"
    assert out["size"]["raw"] == 0 and out["size"]["bytes"] == sum(f.stat().st_size for f in evidence.rglob("*") if f.is_file())
    assert not any((evidence / rel).exists() for rel in raws)
    for kept in ("status.json", "input.cir", "result.txt"):                 # the runs' records and results stay
        assert (evidence / "work" / "runs" / j.id / kept).is_file()
    manifest = _manifest(p, cid)
    assert set(raws) <= set(manifest["files"])                              # with the hashes as captured
    [note] = manifest["pruned"]
    assert note["by"] == "kadomoto" and note["files"] == raws and note["bytes"] == size["raw"] and note["t"]
    record = p.cs.get("p", cid)["evidence"]
    assert record["pruned"] == {"t": note["t"], "by": "kadomoto", "files": len(raws), "bytes": size["raw"]}
    read = p.cs.evidence("p", cid, check=True)                              # a reader says pruned, not missing
    assert read["check"]["pruned"] == raws and read["check"]["missing"] == [] and read["check"]["changed"] == []
    assert read["check"]["ok"] == len(manifest["files"]) - len(raws)
    job = next(x for x in read["jobs"] if x["id"] == j.id)
    assert job["pruned"] == ["nested/trace.raw", "out.raw"]
    (evidence / "work" / "runs" / j.id / "result.txt").write_text("edited by hand\n")
    (evidence / "work" / "runs" / j.id / "input.cir").unlink()
    check = client.get(f"/api/p/p/changesets/{cid}/evidence?check=true").json()["check"]
    assert check["changed"] == [f"work/runs/{j.id}/result.txt"] and check["missing"] == [f"work/runs/{j.id}/input.cir"]
    again = client.post(f"/api/p/p/changesets/{cid}/evidence/prune").json()
    assert again["pruned"]["files"] == 0 and len(_manifest(p, cid)["pruned"]) == 1
    p.cs.revert("p", cid)                                                   # a revert needs none of it
    assert p.cs.get("p", cid)["status"] == "reverted" and value(p.srv) == "1k"
