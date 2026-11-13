"""wait_for: an agent waits for the human side (selection, the changeset applied or discarded, a cell edited) or a job,
instead of polling from outside."""
import json
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pytest  # noqa: E402

from undine.agents import agent_tools as T  # noqa: E402
import test_changeset as tc  # noqa: E402


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(T, "WAIT_POLL_S", 0.02)
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:test", owner="mcp:s", owner_kind="mcp")
    yield root, srv, cs, ctx
    shutil.rmtree(root)


def wait_in_background(ctx, **args):
    box = {}
    th = threading.Thread(target=lambda: box.update(zip(("text", "err"), T.call(ctx, "wait_for", args))))
    th.start()
    time.sleep(0.15)
    return th, box


def test_waits_for_the_person_to_select_something(env):
    root, srv, cs, ctx = env
    th, box = wait_in_background(ctx, events=["selection"], timeout_s=5)
    srv.selection["p"] = {"lib": "l", "cell": "top", "view": "schematic", "selection": [{"k": "i", "id": "R1"}], "t": "now"}
    th.join()
    out = json.loads(box["text"])
    assert not box["err"] and out["event"] == "selection" and out["selection"]["selection"] == [{"k": "i", "id": "R1"}]


def test_waits_for_the_changeset_to_be_settled(env):
    root, srv, cs, ctx = env
    T.call(ctx, "open_changeset", {"title": "t", "reason": "r"})
    srv.leases.release_owner(ctx.owner)                                         # the person can act once the agent is idle
    th, box = wait_in_background(ctx, events=["changeset", "selection"], timeout_s=5)
    cs.discard("p", ctx.changeset)
    th.join()
    assert json.loads(box["text"]) | {"waited_s": 0} == {"event": "changeset", "status": "discarded", "waited_s": 0}


def test_waits_for_a_job_and_a_cell_edited_in_the_real_design(env):
    root, srv, cs, ctx = env
    run = srv.jobs.runs_dir(srv.project("p").path) / "job-1"
    run.mkdir(parents=True)
    (run / "status.json").write_text(json.dumps({"id": "job-1", "state": "running"}))
    th, box = wait_in_background(ctx, events=["job:job-1"], timeout_s=5)
    (run / "status.json").write_text(json.dumps({"id": "job-1", "state": "done"}))
    th.join()
    assert json.loads(box["text"])["state"] == "done"
    text, err = T.call(ctx, "wait_for", {"events": ["job:job-1"]})             # already over: no waiting
    assert json.loads(text)["waited_s"] == 0
    sch = srv.project("p").library("l").path / "top.sch"
    th, box = wait_in_background(ctx, events=["design:l/top"], timeout_s=5)
    sch.write_text(sch.read_text() + "\n")
    th.join()
    assert json.loads(box["text"])["event"] == "design:l/top"


def test_a_quiet_wait_times_out_and_a_bad_event_is_refused(env):
    root, srv, cs, ctx = env
    text, err = T.call(ctx, "wait_for", {"events": ["selection"], "timeout_s": 1})
    assert not err and json.loads(text)["timeout"] is True
    for bad in (["sometime"], ["design:l"], ["job:nope"], ["changeset"]):          # no changeset is open yet
        text, err = T.call(ctx, "wait_for", {"events": bad, "timeout_s": 1})
        assert err, bad
    text, err = T.call(ctx, "open_changeset", {"title": "t", "reason": "r"})
    text, err = T.call(ctx, "batch", {"ops": [{"tool": "wait_for", "args": {"events": ["selection"]}}]})
    assert err and "cannot be part of batch" in text
