"""Phase 4 of docs/design/packages.md, slice 3: agent tools for packages and dependencies, an agent's dependency
operation recorded as *needs fetch* without fetching, the person's Fetch from the review replaying it, and the notice
the agent sees after that fetch.

Agents propose, never fetch: propose_dependency runs with nothing from a remote allowed; what needs a fetch is
recorded in the changeset's project log (URL, commit or rev, size, licence, package, version), nothing written or
installed. A person's Fetch (a confirmation of kind 'dependency' with the changeset and the operation's id, bound to
the id and a hash of the operation) runs the fetch job, which replays the operation, marks it installed and appends a
notice to the changeset; wait_for(event='changeset') returns it. No tool returns a token.

Nothing here reaches the network: remotes are made-up URLs that git rewrites to local repositories (test_pkg_tokens's
Remotes)."""
import json
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402
import test_changeset as tc  # noqa: E402
from test_pkg_index import World  # noqa: E402
from test_pkg_tokens import (HOST, _confirm, _index_cs_project, _plain, _remote_package, _tools_here,  # noqa: E402,F401
                             _wait, remotes)
from undine.agents import agent, agent_tools as T, mcp  # noqa: E402
from undine.server.api import create_app  # noqa: E402
from undine.store import changeset as changesetmod, deps, gitsrc  # noqa: E402
from undine.store import index as ix  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


class NoFetch:
    """A git fetch (gitsrc._fetch_git, what gitsrc.fetch runs when the kept repository lacks the commit) and
    Index.update fail the test while `on`: an agent's path never fetches."""

    def __init__(self, monkeypatch):
        self.on = False
        real_fetch, real_update = gitsrc._fetch_git, ix.Index.update

        def fetch(*a, **k):
            assert not self.on, f"an agent's path fetched {a[:3]}"
            return real_fetch(*a, **k)

        def update(idx, *a, **k):
            assert not self.on, f"an agent's path fetched the index {idx.url}"
            return real_update(idx, *a, **k)
        monkeypatch.setattr(gitsrc, "_fetch_git", fetch)
        monkeypatch.setattr(ix.Index, "update", update)


@pytest.fixture
def nofetch(monkeypatch) -> NoFetch:
    return NoFetch(monkeypatch)


class Tokens:
    """Every token the server issues (Confirmations.issue wrapped), and every agent-facing result, to scan."""

    def __init__(self, srv, monkeypatch):
        self.issued: list[str] = []
        self.results: list[str] = []
        real = srv.confirmations.issue

        def issue(binding):
            token, expires = real(binding)
            self.issued.append(token)
            return token, expires
        monkeypatch.setattr(srv.confirmations, "issue", issue)

    def clean(self) -> None:
        assert self.issued, "the flow issued no token: nothing was checked"
        for text in self.results:
            for t in self.issued:
                assert t not in text, "a tool result carried a token"
            assert '"token"' not in text and "'token'" not in text, text[:300]


def _agent(srv, name: str = "a") -> T.ToolCtx:
    return T.ToolCtx(srv, "p", actor=f"agent:{name}", owner=f"mcp:s1:{name}", owner_kind="mcp")


def _call(ctx, tool: str, args: dict, tokens: Tokens | None = None, ok: bool = True):
    text, err = T.call(ctx, tool, args)
    if tokens is not None:
        tokens.results.append(text)
    assert err is not ok, text
    return json.loads(text) if not err and text.startswith("{") else text


def _open(ctx, tokens=None) -> str:
    return _call(ctx, "open_changeset", {"title": "use amp", "reason": "the buffer needs an amplifier"}, tokens)["id"]


def _propose(ctx, tokens=None, ok=True, **over) -> dict:
    args = {"action": "add", "name": "amp", "version": "^1", "reason": "the output stage needs an amplifier", **over}
    return _call(ctx, "propose_dependency", {k: v for k, v in args.items() if v is not None}, tokens, ok)


def _pending(c: TestClient, cid: str) -> list[dict]:
    r = c.get(f"/api/p/p/changesets/{cid}")
    assert r.status_code == 200, r.text
    return r.json().get("pending_deps") or []


def _person_fetch(c: TestClient, plan: dict) -> dict:
    r = _confirm(c, plan)
    assert r.status_code == 200, r.text
    r = c.post("/api/p/p/deps/fetch", json={"token": r.json()["token"]})
    assert r.status_code == 200, r.text
    return _wait(c, "p", r.json()["job"])


def _quick_checks(monkeypatch) -> None:
    """run_checks without a PDK's decks: the verify slots as they are, none started (the schematic rows still run)."""
    monkeypatch.setattr(changesetmod.Changesets, "verify", lambda self, project, cid, fresh=False, commit=True: {})


# ------------------------------------------------------------------ acceptance


def test_agent_add_records_needs_fetch_never_fetches(tmp_path, remotes, nofetch):
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    work = srv.project(f"p~{cid}")
    before = {n: (work.path / n).read_bytes() for n in ("project.json", deps.LOCK)}
    nofetch.on = True
    out = _propose(me)
    assert out["state"] == "needs_fetch" and out["op_id"].startswith("dep-"), out
    [need] = out["needs"]
    assert (need["url"], need["commit"], need["size"], need["license"], need["package"], need["version"]) == \
        (line["git"], line["commit"], 4096, "MIT", "amp", "1.0.0")
    assert "a person fetches it" in out["note"] and "wait_for" in out["note"]
    # nothing written, installed or fetched
    assert {n: (work.path / n).read_bytes() for n in ("project.json", deps.LOCK)} == before
    assert not deps.dep_dir(work.path, "amp").exists() and not gitsrc.repo_path(line["git"]).exists()
    # the record says needs_fetch, with what a person's fetch needs
    [rec] = [r for r in srv.oplog(f"p~{cid}", changesetmod.PROJECT_LOG).read(limit=100) if r.get("changeset") == cid]
    [e] = rec["effects"]
    assert (rec["op"], rec["actor"], e["k"], e["state"], e["name"], e["op_id"]) == \
        ("dep_add", "agent:a", "d", "needs_fetch", "amp", out["op_id"])
    assert e["needs"][0]["url"] == line["git"] and e["needs"][0]["size"] == 4096
    assert "mcp:" not in json.dumps(rec)                 # the lease's owner only as a hash
    assert rec["plan"]["request"] == {"op": "add", "name": "amp", "package": "amp", "version": "^1",
                                      "changeset": cid, "op_id": out["op_id"],
                                      "reason": "the output stage needs an amplifier"}
    # the review shows it waiting, and the agent's own status too
    [p] = _pending(c, cid)
    assert p["op_id"] == out["op_id"] and p["plan"]["fetches"][0]["url"] == line["git"] and "owner" not in p
    st = _call(me, "get_dependency_status", {})
    assert [x["op_id"] for x in st["waiting_for_fetch"]] == [out["op_id"]] and "data" in st["data_rule"]
    g = _call(me, "get_changeset", {})
    assert "waits for a person's fetch" in json.dumps(g["changes"]), g["changes"]
    shutil.rmtree(root, ignore_errors=True)


def test_agent_continues_after_persons_fetch(tmp_path, remotes, nofetch, monkeypatch):
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    tokens = Tokens(srv, monkeypatch)
    me = _agent(srv)
    cid = _open(me, tokens)
    nofetch.on = True
    op_id = _propose(me, tokens)["op_id"]
    nofetch.on = False
    waited: dict = {}

    def wait():
        waited["out"] = _call(me, "wait_for", {"events": ["changeset"], "timeout_s": 60}, tokens)
    th = threading.Thread(target=wait)
    th.start()
    time.sleep(1.0)                                      # waiting already, holding its lease
    assert srv.leases.get(cid).owner == "mcp:s1:a"
    [p] = _pending(c, cid)
    job = _person_fetch(c, p["plan"])
    assert job["state"] == "done", job
    assert job["result"]["state"] == "installed" and job["result"]["op_id"] == op_id
    th.join(30)
    assert not th.is_alive()
    out = waited["out"]
    assert out["event"] == "changeset" and out["status"] == "open", out
    [n] = out["notices"]
    assert n["kind"] == "fetched" and n["op_id"] == op_id and n["name"] == "amp" and "installed" in n["text"]
    # installed in the working copy, recorded as the agent's operation the person fetched
    work = srv.project(f"p~{cid}")
    assert (deps.dep_dir(work.path, "amp") / "amp_cell.sym").is_file()
    assert deps.read_lock(work.path)["libraries"]["amp"]["version"] == "1.0.0"
    assert not deps.dep_dir(srv.project("p").path, "amp").exists()          # the project as it was
    recs = [r for r in srv.oplog(f"p~{cid}", changesetmod.PROJECT_LOG).read(limit=100) if r.get("changeset") == cid]
    assert [(r["op"], r["effects"][0]["state"], r.get("replays"), r["actor"]) for r in recs] == \
        [("dep_add", "needs_fetch", None, "agent:a"), ("dep_add", "installed", op_id, "agent:a")]
    assert recs[1]["fetched_by"] == "user"
    assert _pending(c, cid) == []
    # the agent goes on: places a package cell and runs the checks
    syms = _call(me, "list_symbols", {"lib": "amp"}, tokens)
    assert "amp/amp_cell.sym" in syms["symbols"]
    _call(me, "add_instance", {"lib": "l", "cell": "top", "symbol": "amp/amp_cell.sym",
                               "reason": "the amplifier the person fetched"}, tokens)
    _quick_checks(monkeypatch)
    checks = _call(me, "run_checks", {}, tokens)
    assert any(r["kind"] == "schematic" and r["cell"] == "top" for r in checks["checks"]), checks
    st = _call(me, "get_dependency_status", {}, tokens)
    assert st["waiting_for_fetch"] == [] and [x["kind"] for x in st["notices"]] == ["fetched"]
    assert "notices_not_given" not in st                 # wait_for gave it already: once per agent
    # the review no longer calls the proposal waiting: it was fetched and installed (D)
    [lc] = [x for x in c.get(f"/api/p/p/changesets/{cid}").json()["changes"] if x["lib"] == "@project"]
    assert [(e["state"], e.get("outcome")) for op in lc["ops"] for e in op["effects"]] == \
        [("needs_fetch", "installed"), ("installed", None)]
    tl = c.get(f"/api/p/p/changesets/{cid}/timeline").json()
    assert any(d["effect"].get("outcome") == "installed" for o in tl["ops"] for d in o["effect_data"])
    g = _call(me, "get_changeset", {}, tokens)
    assert "a person fetched it: installed" in json.dumps(g["changes"]) and "nothing installed yet" not in \
        json.dumps(g["changes"])
    tokens.clean()
    shutil.rmtree(root, ignore_errors=True)


def test_no_tool_returns_a_token(tmp_path, remotes, nofetch, monkeypatch):
    """Every tool of the flow, and every tool that reads the changeset or the dependencies afterwards: no result
    carries a token the server issued, nor a field named token; no tool's schema names one."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    tokens = Tokens(srv, monkeypatch)
    me = _agent(srv)
    cid = _open(me, tokens)
    _call(me, "search_packages", {"query": "amp"}, tokens)
    _call(me, "get_package", {"name": "amp"}, tokens)
    _propose(me, tokens)
    [p] = _pending(c, cid)
    assert _person_fetch(c, p["plan"])["state"] == "done"
    for tool, args in (("get_dependency_status", {}), ("get_changeset", {}), ("list_changesets", {}),
                       ("wait_for", {"events": ["changeset"], "timeout_s": 1}), ("get_history", {"lib": "l",
                                                                                               "cell": "top"})):
        _call(me, tool, args, tokens)
    tokens.clean()
    for t in T.TOOLS:                                   # no tool takes one
        assert not [k for k in t["input_schema"].get("properties") or {} if "token" in k], t["name"]
    for name in ("search_packages", "get_package", "get_dependency_status", "propose_dependency"):
        schema = (json.dumps(T.BY_NAME[name]["input_schema"]) + T.BY_NAME[name]["description"]).lower()
        assert "token" not in schema and "confirm" not in schema and "trusted" not in schema, name
    shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------ reading the indexes


def test_search_packages_without_index_copy_fetches_nothing(tmp_path, nofetch):
    w = World(tmp_path / "local")
    w.default()
    w.publish("amp", "1.0.0")
    root, srv, cs = tc.setup()
    srv.dep_roots = [tmp_path.resolve()]
    me = _agent(srv)
    nofetch.on = True
    for tool, args in (("search_packages", {"query": "amp"}), ("get_package", {"name": "amp"})):
        text = _call(me, tool, args, ok=False)
        assert "no local copy" in text and "a person refreshes the index in the Packages dialog" in text, text
    assert not ix.Index(str(w.index)).fetched()
    nofetch.on = False
    ix.Index(str(w.index)).update()                     # a person's refresh
    nofetch.on = True
    got = _call(me, "search_packages", {"query": "amp"})
    assert [r["name"] for r in got["packages"]] == ["amp"] and got["total"] == 1 and "data" in got["data_rule"]
    info = _call(me, "get_package", {"name": "amp"})
    assert info["newest"] == "1.0.0" and [v["version"] for v in info["versions"]] == ["1.0.0"]
    assert "propose_dependency" in info["next"]
    assert "lists no package" in _call(me, "get_package", {"name": "nothing"}, ok=False)
    shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------ refusals


def test_propose_dependency_outside_changeset_refused(tmp_path, remotes, nofetch):
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    nofetch.on = True
    assert "no changeset is open" in _propose(me, ok=False)
    _open(me)
    assert "reason is required" in _propose(me, reason="", ok=False)
    assert "action: one of add, update, remove" in _propose(me, action="install", ok=False)
    assert "name:" in _propose(me, name="", ok=False)
    assert "not git" in _propose(me, action="update", git="https://x/y.git", ok=False)
    # not in a batch, which holds the changesets' lock
    text = _call(me, "batch", {"ops": [{"tool": "propose_dependency", "args": {
        "action": "add", "name": "amp", "reason": "r"}}], "reason": "r"}, ok=False)
    assert "propose_dependency" in text
    # another agent's changeset is not written with changeset=
    other = _agent(srv, "b")
    _open(other)
    assert "does not take changeset" in _call(other, "propose_dependency", {
        "action": "add", "name": "amp", "reason": "r", "changeset": me.changeset}, ok=False)
    # an index with no local copy: refused as the reads are, nothing fetched
    shutil.rmtree(ix.Index(str(w.index)).repo)
    assert "a person refreshes the index in the Packages dialog" in _propose(me, ok=False)
    assert [r for r in srv.oplog(f"p~{me.changeset}", changesetmod.PROJECT_LOG).read(limit=100)
            if r.get("changeset") == me.changeset] == []
    shutil.rmtree(root, ignore_errors=True)


def test_a_dependency_the_store_has_is_installed_at_once(tmp_path, nofetch):
    """A local repository under --dep-root needs no person's fetch: the agent's add is installed in the preview."""
    root, srv, cs = tc.setup()
    srv.dep_roots = [tmp_path.resolve()]
    local, commit = _plain(tmp_path / "loc", ["buf"])
    me = _agent(srv)
    cid = _open(me)
    out = _propose(me, name="loc", git=str(local), rev="v1", version=None)
    assert out["state"] == "installed" and out["result"]["commit"] == commit, out
    assert (deps.dep_dir(srv.project(f"p~{cid}").path, "loc") / "buf.sym").is_file()
    assert "list_symbols lib=loc" in out["note"]
    shutil.rmtree(root, ignore_errors=True)


def test_dependency_status_reads_other_changeset(tmp_path, remotes, nofetch):
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    a, b = _agent(srv, "a"), _agent(srv, "b")
    cid = _open(a)
    op_id = _propose(a)["op_id"]
    _open(b)
    st = _call(b, "get_dependency_status", {"changeset": cid})
    assert st["changeset"] == cid and st["project"] == f"p~{cid}"
    assert [x["op_id"] for x in st["waiting_for_fetch"]] == [op_id] and st["waiting_for_fetch"][0]["by"] == "agent:a"
    assert _call(b, "get_dependency_status", {})["waiting_for_fetch"] == []        # its own has none
    assert srv.leases.get(cid).owner == "mcp:s1:a"                                 # the reader took no lease
    assert "get_dependency_status" in T.READ_OTHER and "propose_dependency" in T.BATCH_EXCLUDED
    shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------ cancelled operations


def test_restore_cancels_pending_op(tmp_path, remotes, nofetch):
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    op_id = _propose(me)["op_id"]
    [p] = _pending(c, cid)
    r = _confirm(c, p["plan"])                          # the person's token for it, then the agent restores
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    _call(me, "restore_changeset", {"to": 0})
    assert _pending(c, cid) == []
    recs = srv.oplog(f"p~{cid}", changesetmod.PROJECT_LOG).read(limit=100)
    assert recs[-1]["op"] == "restore" and recs[-1]["cancelled"] == [op_id]
    r = c.post("/api/p/p/deps/fetch", json={"token": tok})
    assert r.status_code == 409 and r.json()["code"] == "not_pending", r.text
    r = c.post("/api/p/p/deps/fetch", json={"token": tok})               # and the token was used up
    assert r.status_code == 403, r.text
    r = _confirm(c, p["plan"])
    assert r.status_code == 409 and r.json()["code"] == "not_pending", r.text
    assert not gitsrc.repo_path(line["git"]).exists()                    # nothing fetched
    # a discard cancels one too
    op2 = _propose(me)["op_id"]
    [p2] = _pending(c, cid)
    assert p2["op_id"] == op2
    srv.leases.release(cid, me.owner)
    cs.discard("p", cid)
    r = _confirm(c, p2["plan"])
    assert r.status_code == 409 and r.json()["code"] == "not_pending", r.text
    # and an adopt: the changeset applied with the operation still waiting, which the project never gets
    me.changeset = None
    cid3 = _open(me)
    _propose(me)
    _call(me, "add_instance", {"lib": "l", "cell": "top", "symbol": "r.sym", "reason": "one more"})
    [p3] = _pending(c, cid3)
    srv.leases.release(cid3, me.owner)
    assert cs.apply("p", cid3)["status"] == "applied"
    r = _confirm(c, p3["plan"])
    assert r.status_code == 409 and r.json()["code"] == "not_pending", r.text
    [lc] = [x for x in cs.get("p", cid3)["changes"] if x["lib"] == "@project"]   # the evidence: never fetched
    assert [e.get("outcome") for op in lc["ops"] for e in op["effects"]] == ["cancelled"]
    assert "amp" not in srv.project("p").library_specs()
    shutil.rmtree(root, ignore_errors=True)


def test_a_token_binds_the_operation_and_its_fields_come_from_the_record(tmp_path, remotes, nofetch):
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    op_id = _propose(me)["op_id"]
    [p] = _pending(c, cid)
    # the body's fields cannot change what is replayed: the record's are used
    r = _confirm(c, p["plan"], name="other", package="other", version="^9")
    assert r.status_code == 200 and r.json()["plan"]["request"]["name"] == "amp", r.text
    # an unknown operation id
    r = _confirm(c, p["plan"], op_id="dep-0000000000")
    assert r.status_code == 409 and r.json()["code"] == "not_pending", r.text
    # the plan changed since the review showed it (a newer version in the index refreshed meanwhile): the new plan
    _remote_package(w, remotes, "amp", "1.1.0", line["git"])
    ix.Index(str(w.index)).update()
    r = _confirm(c, p["plan"])
    assert r.status_code == 409 and r.json()["code"] == "needs_fetch", r.text
    plan = r.json()["plan"]
    assert [f["version"] for f in plan["fetches"]] == ["1.1.0"] and plan["request"]["op_id"] == op_id
    job = _person_fetch(c, plan)
    assert job["state"] == "done", job
    assert deps.read_lock(srv.project(f"p~{cid}").path)["libraries"]["amp"]["version"] == "1.1.0"
    # done: a second confirmation of it is refused
    assert _confirm(c, plan).json()["code"] == "not_pending"
    shutil.rmtree(root, ignore_errors=True)


def test_another_writers_lease_is_busy_and_a_second_click_only_installs(tmp_path, remotes, nofetch, monkeypatch):
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    monkeypatch.setattr(changesetmod, "REPLAY_RETRY_S", 0.5)
    me = _agent(srv)
    cid = _open(me)
    op_id = _propose(me)["op_id"]
    srv.leases.release(cid, me.owner)
    srv.leases.acquire(cid, "turn:t1", "turn", "assistant")          # someone else writes the changeset now
    [p] = _pending(c, cid)
    job = _person_fetch(c, p["plan"])
    assert job["state"] == "failed" and job["code"] == "busy" and "Fetch again" in job["error"], job
    assert gitsrc.repo_path(line["git"]).exists()                    # fetched into the store all the same
    assert [x["op_id"] for x in _pending(c, cid)] == [op_id]          # still waiting
    srv.leases.release(cid, "turn:t1")
    nofetch.on = True                                                # the second click fetches nothing
    job = _person_fetch(c, p["plan"])
    assert job["state"] == "done" and job["result"]["state"] == "installed", job
    assert (deps.dep_dir(srv.project(f"p~{cid}").path, "amp") / "amp_cell.sym").is_file()
    shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------ lock order


def test_propose_and_replay_do_not_deadlock(tmp_path, remotes, monkeypatch):
    """A person's replay holds the working copy's dependency lock while it fetches, then takes the changesets' lock to
    commit. An agent's propose_dependency in the same changeset meanwhile never holds the changesets' lock while it
    asks for the dependency lock, and does not wait for it: it answers busy at once, and the replay finishes."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    _propose(me)
    [p] = _pending(c, cid)
    in_fetch, go_on = threading.Event(), threading.Event()
    real_fetch = gitsrc.fetch

    def slow_fetch(*a, **k):
        in_fetch.set()
        assert go_on.wait(30)
        return real_fetch(*a, **k)
    monkeypatch.setattr(gitsrc, "fetch", slow_fetch)
    order: list[tuple[str, bool]] = []
    real_lock = deps._deps_lock

    def deps_lock(path, wait=True):
        order.append((threading.current_thread().name, srv.changeset_lock._is_owned()))
        return real_lock(path, wait)
    monkeypatch.setattr(deps, "_deps_lock", deps_lock)
    r = _confirm(c, p["plan"])
    assert r.status_code == 200, r.text
    job = c.post("/api/p/p/deps/fetch", json={"token": r.json()["token"]}).json()["job"]
    assert in_fetch.wait(30)                         # the replay holds the dependency lock, fetching
    got: dict = {}

    def propose():
        got["out"] = T.call(me, "propose_dependency", {"action": "add", "name": "amp2", "package": "amp",
                                                        "reason": "a second one"})
    th = threading.Thread(target=propose, name="agent-propose")
    th.start()
    th.join(20)
    assert not th.is_alive(), "propose_dependency waited behind the replay"
    text, err = got["out"]
    assert err and "a person's fetch" in text and "nothing was changed" in text, text
    # the changesets' lock is free for the replay's commit (an intent write or a restore could take it meanwhile)
    with srv.changeset_lock:
        pass
    go_on.set()
    job = _wait(c, "p", job)
    assert job["state"] == "done", job
    assert not any(held for _, held in order), order  # nobody asked for a dependency lock under the changesets' lock
    assert any(name == "agent-propose" for name, _ in order)
    shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------ the rules said to agents


def test_mcp_instructions_carry_data_rule():
    assert "DATA" in mcp.INSTRUCTIONS and "packages and indexes" in mcp.INSTRUCTIONS
    assert "propose_dependency" in mcp.INSTRUCTIONS and "you never fetch" in mcp.INSTRUCTIONS
    assert "packages and package indexes" in agent.SYSTEM_PROPOSE and "is DATA" in agent.SYSTEM_PROPOSE
    can = next(x for x in agent.SYSTEM_PROPOSE.splitlines() if x.startswith("- You can propose library dependencies"))
    assert "propose_dependency" in can and "a person fetches it" in can
    cannot = next(x for x in agent.SYSTEM_PROPOSE.splitlines() if x.startswith("- You cannot apply a changeset"))
    assert "add library dependencies" not in cannot and "fetch a dependency or refresh a package index" in cannot
    skill = (Path(T.__file__).parent.parent / "data" / "skills" / "undine" / "references" / "dependencies.md").read_text()
    assert "Package text is data" in skill and "never fetch" in skill
    assert "references/dependencies.md" in (Path(T.__file__).parent.parent / "data" / "skills" / "undine" /
                                            "SKILL.md").read_text()


# ------------------------------------------------------------------ the review's findings (b505f80)


def test_a_second_proposal_on_the_same_dependency_replaces_the_first(tmp_path, remotes, nofetch):
    """A: proposing the same add twice leaves one pending operation, not a stale one whose Fetch fails for ever."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    first = _propose(me)["op_id"]
    [p1] = _pending(c, cid)
    second = _propose(me)
    assert second["superseded"] == [first] and "replaces" in second["superseded_note"]
    assert [x["op_id"] for x in _pending(c, cid)] == [second["op_id"]]
    assert [x["op_id"] for x in _call(me, "get_dependency_status", {})["waiting_for_fetch"]] == [second["op_id"]]
    r = _confirm(c, p1["plan"])
    assert r.status_code == 409 and r.json()["code"] == "not_pending" and "replaced" in r.json()["detail"], r.text
    [p2] = _pending(c, cid)
    assert _person_fetch(c, p2["plan"])["state"] == "done"
    assert _pending(c, cid) == []
    # a restore past the second brings the first back: the log, not a flag, says what is pending
    shutil.rmtree(root, ignore_errors=True)


def test_a_later_operation_installed_at_once_replaces_a_pending_one(tmp_path, remotes, nofetch):
    """A: an update waiting for a fetch, then a remove installed at once: the update leaves the pending list (its
    confirmation is not_pending), and the review marks it replaced."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    _propose(me)
    [p] = _pending(c, cid)
    assert _person_fetch(c, p["plan"])["state"] == "done"            # amp 1.0.0 installed
    _remote_package(w, remotes, "amp", "1.1.0", line["git"])
    ix.Index(str(w.index)).update()                                  # a person's refresh
    up = _propose(me, action="update", version="^1.1")
    assert up["state"] == "needs_fetch", up
    [pu] = _pending(c, cid)
    gone = _propose(me, action="remove", version=None)
    assert gone["state"] == "installed" and gone["superseded"] == [up["op_id"]], gone
    assert _pending(c, cid) == []
    r = _confirm(c, pu["plan"])
    assert r.status_code == 409 and r.json()["code"] == "not_pending", r.text
    [lc] = [x for x in c.get(f"/api/p/p/changesets/{cid}").json()["changes"] if x["lib"] == "@project"]
    assert ("needs_fetch", "superseded") in [(e["state"], e.get("outcome")) for op in lc["ops"] for e in op["effects"]]
    shutil.rmtree(root, ignore_errors=True)


def test_a_pending_op_that_can_no_longer_apply_is_dropped_with_a_notice(tmp_path, remotes, nofetch):
    """A: the working copy changed under a pending add outside the recorded operations (the name is taken): the
    person's confirmation drops it with a fetch_failed notice the agent gets, and it is not_pending from then on."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    op_id = _propose(me)["op_id"]
    [p] = _pending(c, cid)
    work = srv.project(f"p~{cid}")
    work.meta.setdefault("libraries", {})["amp"] = {"path": "amp"}
    (work.path / "amp").mkdir()
    work.save()
    r = _confirm(c, p["plan"])
    assert r.status_code == 409 and r.json()["code"] == "not_pending" and "can no longer apply" in r.json()["detail"], \
        r.text
    assert _pending(c, cid) == []
    r = _confirm(c, p["plan"])                                        # the same answer, not a 400 at every click
    assert r.status_code == 409 and r.json()["code"] == "not_pending", r.text
    out = _call(me, "wait_for", {"events": ["changeset"], "timeout_s": 5})
    [n] = out["notices"]
    assert n["kind"] == "fetch_failed" and n["op_id"] == op_id and n["closes"] and "dropped" in n["text"]
    assert not gitsrc.repo_path(line["git"]).exists()
    shutil.rmtree(root, ignore_errors=True)


def test_wait_for_returns_a_notice_that_came_before_it_once_per_agent(tmp_path, remotes, nofetch):
    """B: the person fetched before the agent began to wait: wait_for answers at once with the notice, and only once;
    another agent reading the changeset gets it once too; get_dependency_status gives it as well."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    op_id = _propose(me)["op_id"]
    [p] = _pending(c, cid)
    assert _person_fetch(c, p["plan"])["state"] == "done"
    t0 = time.monotonic()
    out = _call(me, "wait_for", {"events": ["changeset"], "timeout_s": 30})
    assert time.monotonic() - t0 < 5 and out["waited_s"] == 0, out
    assert [n["op_id"] for n in out["notices"]] == [op_id] and out["notices"][0]["kind"] == "fetched"
    again = _call(me, "wait_for", {"events": ["changeset"], "timeout_s": 1})
    assert again["event"] is None and again["timeout"] and "notices" not in again
    assert "notices_not_given" not in _call(me, "get_dependency_status", {})
    reader = _agent(srv, "r")
    st = _call(reader, "get_dependency_status", {"changeset": cid})
    assert st["notices_not_given"] == 1                  # it lists them, and gives none (wait_for gives them)
    out = _call(reader, "wait_for", {"events": ["changeset"], "timeout_s": 5, "changeset": cid})
    assert [n["op_id"] for n in out["notices"]] == [op_id]
    out = _call(reader, "wait_for", {"events": ["changeset"], "timeout_s": 1, "changeset": cid})
    assert out["event"] is None and "notices" not in out
    third = _agent(srv, "t")
    out = _call(third, "wait_for", {"events": ["changeset"], "timeout_s": 5, "changeset": cid})
    assert [n["op_id"] for n in out["notices"]] == [op_id]
    shutil.rmtree(root, ignore_errors=True)


def test_the_assistants_next_turn_hears_of_a_notice_that_came_mid_turn(tmp_path, remotes, monkeypatch):
    """C: the person fetches while the assistant's turn still runs (the replay acts as the turn's owner); the
    end-of-turn snapshot does not swallow the notice: the next turn's changeset state has it, and the one after does
    not repeat it."""
    from http.server import HTTPServer
    from undine.agents import llm
    import test_agent as ta
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    httpd = HTTPServer(("127.0.0.1", 0), ta.Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    srv.gateway = llm.Gateway(root, "server")
    srv.gateway.switch("server", "m", f"http://127.0.0.1:{httpd.server_port}/v1")
    conv = {"id": "chat-deps", "connection": {"kind": "server", "model": "m"}, "messages": []}
    real_call = T.call
    fetched: list[dict] = []

    def call(ctx, name, args, *a, **k):
        out = real_call(ctx, name, args, *a, **k)
        if name == "propose_dependency":                       # the person clicks Fetch at once, mid-turn
            [p] = _pending(c, ctx.changeset)
            fetched.append(_person_fetch(c, p["plan"]))
        return out
    monkeypatch.setattr(agent.T, "call", call)
    ta.SCRIPT[:] = [
        {"content": None, "tool_calls": [ta.call("open_changeset", title="use amp", reason="r"),
                                         {"id": "c-propose", "type": "function", "function": {
                                             "name": "propose_dependency", "arguments": json.dumps(
                                                 {"action": "add", "name": "amp", "version": "^1",
                                                  "reason": "an amplifier"})}}]},
        {"content": "proposed amp; a person fetches it"},
    ]
    res = agent.run(srv, "p", srv.project("p").path, conv, [], "use the amp package", "agent:assistant", {},
                    owner="turn:t1", turn="t1")
    assert fetched and fetched[0]["state"] == "done", fetched
    assert fetched[0]["result"]["notice"]["kind"] == "fetched"
    srv.leases.release(res["changeset"], "turn:t1")
    ta.SEEN.clear()
    ta.SCRIPT[:] = [{"content": "placing it"}]
    agent.run(srv, "p", srv.project("p").path, conv, [], "go on", "agent:assistant", {}, owner="turn:t2", turn="t2")
    first = json.dumps(ta.SEEN[0]["messages"][-1])
    assert "notice (fetched)" in first and "installed" in first, first
    srv.leases.release(res["changeset"], "turn:t2")
    ta.SEEN.clear()
    ta.SCRIPT[:] = [{"content": "nothing"}]
    agent.run(srv, "p", srv.project("p").path, conv, [], "anything else?", "agent:assistant", {}, owner="turn:t3",
              turn="t3")
    assert "notice" not in json.dumps(ta.SEEN[0]["messages"][-1])
    httpd.shutdown()
    shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------ the second review's findings (f0ed0d7)


def _deps_ops(g: dict) -> list[tuple]:
    return [(e.get("state"), e.get("outcome")) for lc in g["changes"] if lc["lib"] == "@project"
            for op in lc["ops"] for e in op["effects"]]


def test_closing_after_a_fetch_or_a_replaced_proposal_keeps_nothing_and_warns_nothing(tmp_path, remotes, nofetch):
    """1: the outcomes come from the log whatever the status, so the closure's check of the changes before and after
    the status moved agrees: no "changed during closure" warning, the working copy removed; the evidence keeps the
    outcomes."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    # a discard of one whose first proposal a second replaced, and whose second never was fetched
    other = _agent(srv, "b")
    cid2 = _open(other)
    _propose(other)
    _propose(other)
    srv.leases.release(cid2, other.owner)
    out = cs.discard("p", cid2)
    assert not out.get("cleanup_warning") and not out.get("retained"), out
    assert _deps_ops(cs.get("p", cid2)) == [("needs_fetch", "superseded"), ("needs_fetch", "cancelled")]
    # an apply after the person's fetch
    me = _agent(srv)
    cid = _open(me)
    _propose(me)
    [p] = _pending(c, cid)
    assert _person_fetch(c, p["plan"])["state"] == "done"
    srv.leases.release(cid, me.owner)
    out = cs.apply("p", cid)
    assert out["status"] == "applied" and not out.get("cleanup_warning") and not out.get("retained"), out
    g = cs.get("p", cid)
    assert not g.get("cleanup_warning") and g["retained"] is False
    assert _deps_ops(g) == [("needs_fetch", "installed"), ("installed", None)]
    shutil.rmtree(root, ignore_errors=True)


def test_a_failure_that_passes_leaves_the_op_pending(tmp_path, remotes, nofetch, monkeypatch):
    """2: a network failure during the person's Fetch, or a confirmation that fails for a reason of the server's,
    fails and leaves the operation pending: the next Fetch works."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    op_id = _propose(me)["op_id"]
    [p] = _pending(c, cid)
    real = gitsrc._fetch_git

    def unreachable(*a, **k):
        raise gitsrc.GitSourceError("fatal: unable to access: Could not resolve host: git.example.org")
    monkeypatch.setattr(gitsrc, "_fetch_git", unreachable)
    job = _person_fetch(c, p["plan"])
    assert job["state"] == "failed" and "Could not resolve host" in job["error"], job
    assert [x["op_id"] for x in _pending(c, cid)] == [op_id]
    assert cs._record("p", cid).get("notices") in (None, [])
    real_plan = deps.plan_fetch

    def broken(*a, **k):
        raise deps.DepError("cannot read ~/.undine/config.toml: bad TOML")
    monkeypatch.setattr(deps, "plan_fetch", broken)
    r = _confirm(c, p["plan"])
    assert r.status_code == 400 and "config.toml" in r.json()["detail"], r.text
    assert [x["op_id"] for x in _pending(c, cid)] == [op_id]
    monkeypatch.setattr(deps, "plan_fetch", real_plan)
    monkeypatch.setattr(gitsrc, "_fetch_git", real)
    job = _person_fetch(c, p["plan"])
    assert job["state"] == "done" and job["result"]["op_id"] == op_id, job
    shutil.rmtree(root, ignore_errors=True)


def test_a_drop_is_undone_by_a_restore_and_never_marks_an_installed_op(tmp_path, remotes, nofetch):
    """3: the agent's add of amp waits; the agent then makes a library amp, so the add can no longer apply and the
    person's confirmation drops it, in the log. A restore past the library brings it back pending (the drop goes with
    it). A drop of an installed operation is refused under the lock and writes nothing."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    a1 = _propose(me)["op_id"]
    [p1] = _pending(c, cid)
    _call(me, "create_library", {"name": "amp", "reason": "a block of its own"})
    r = _confirm(c, p1["plan"])
    assert r.status_code == 409 and r.json()["code"] == "not_pending" and "can no longer apply" in r.json()["detail"], \
        r.text
    assert _pending(c, cid) == []
    assert ("dropped", None) in _deps_ops(c.get(f"/api/p/p/changesets/{cid}").json())
    _call(me, "restore_changeset", {"to": p1["gseq"]})     # before the library (and the drop)
    assert [x["op_id"] for x in _pending(c, cid)] == [a1]
    assert ("dropped", None) not in _deps_ops(c.get(f"/api/p/p/changesets/{cid}").json())
    # the race a confirmation could lose: the op installed between its read and its drop
    [p1] = _pending(c, cid)
    assert _person_fetch(c, p1["plan"])["state"] == "done"
    before = len(cs._record("p", cid)["notices"])
    with pytest.raises(changesetmod.NotPending):
        cs.drop_pending("p", cid, a1, "amp", "too late")
    assert len(cs._record("p", cid)["notices"]) == before
    assert ("dropped", None) not in _deps_ops(c.get(f"/api/p/p/changesets/{cid}").json())
    shutil.rmtree(root, ignore_errors=True)


def test_a_resolution_refused_leaves_the_op_pending_and_tells_the_agent_once(tmp_path, remotes, nofetch):
    """1 (third review): amp is fetched, so amp2 (the same package) does not resolve: 409 resolution_refused, the op
    stays pending (a later change may let it resolve), and the agent gets one fetch_failed notice that does not close
    it, not one per click. A fetch job that does not resolve is the same."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    _propose(me)
    a2 = _propose(me, name="amp2", package="amp")["op_id"]
    p1, p2 = _pending(c, cid)
    assert _person_fetch(c, p1["plan"])["state"] == "done"
    for _ in range(2):
        r = _confirm(c, p2["plan"])
        assert r.status_code == 409 and r.json()["code"] == "resolution_refused", r.text
    assert [x["op_id"] for x in _pending(c, cid)] == [a2]
    failed = [n for n in cs._record("p", cid)["notices"] if n["kind"] == "fetch_failed"]
    assert len(failed) == 1 and failed[0]["op_id"] == a2 and not failed[0].get("closes")
    assert len(failed[0]["text"]) <= changesetmod.NOTICE_TEXT
    out = _call(me, "wait_for", {"events": ["changeset"], "timeout_s": 5})
    assert [n["kind"] for n in out["notices"]] == ["fetched", "fetch_failed"]
    shutil.rmtree(root, ignore_errors=True)


def test_a_fetch_during_a_batch_step_is_busy_and_never_undone(tmp_path, remotes, nofetch, monkeypatch):
    """2 (third review): a person's Fetch (or a drop) while the agent's batch step runs is busy, so a dry run or a
    failed step that is put back never takes back an install whose notice the agent was given; after the step the
    Fetch only installs."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    monkeypatch.setattr(changesetmod, "REPLAY_RETRY_S", 0.3)
    me = _agent(srv)
    cid = _open(me)
    op_id = _propose(me)["op_id"]
    [p] = _pending(c, cid)
    seen: dict = {}

    def step():
        seen["job"] = _person_fetch(c, p["plan"])              # the person clicks Fetch during the step
        with pytest.raises(changesetmod.Busy):
            cs.drop_pending("p", cid, op_id, "amp", "a drop during the step")
        return []
    out = cs.batch("p", cid, step, dry_run=True)
    assert out["results"] == []
    job = seen["job"]
    assert job["state"] == "failed" and job["code"] == "busy" and "batch step" in job["error"], job
    assert [x["op_id"] for x in _pending(c, cid)] == [op_id]
    assert not [n for n in cs._record("p", cid).get("notices") or [] if n["kind"] == "fetched"]
    assert gitsrc.repo_path(line["git"]).exists()              # fetched into the store all the same
    nofetch.on = True
    job = _person_fetch(c, p["plan"])
    assert job["state"] == "done" and job["result"]["state"] == "installed", job
    shutil.rmtree(root, ignore_errors=True)


def test_two_waits_of_one_agent_never_get_the_same_notice(tmp_path, remotes):
    """3 (third review): the marking is atomic: two calls that peeked the same notices give each once."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    for i in range(4):
        cs.add_notice("p", cid, {"kind": "fetched", "op_id": f"dep-{i}", "name": "amp", "text": f"notice {i}"})
    real = cs.peek_notices.__func__
    peeked = threading.Barrier(2)

    def peek(self, *a, **k):                                  # both calls peek before either marks
        out = real(self, *a, **k)
        try:
            peeked.wait(5)
        except threading.BrokenBarrierError:
            pass
        return out
    monkeypatch_target = changesetmod.Changesets
    old = monkeypatch_target.peek_notices
    monkeypatch_target.peek_notices = peek
    got: list = []
    try:
        ths = [threading.Thread(target=lambda: got.append(cs.take_notices("p", cid, me.owner))) for _ in range(2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(20)
    finally:
        monkeypatch_target.peek_notices = old
    texts = [n["text"] for g in got for n in g]
    assert sorted(texts) == [f"notice {i}" for i in range(4)], got            # each once, none twice
    shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("budget", [1000, 2000])
def test_wait_for_gives_only_the_notices_its_answer_holds(tmp_path, remotes, budget):
    """4a (third review): at a small result budget (MCP undine/max_chars) wait_for gives as many notices as fit
    uncut, marks only those, and says how many more wait; a long text is kept short."""
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    for i in range(5):
        cs.add_notice("p", cid, {"kind": "fetch_failed", "op_id": f"dep-{i}", "name": "amp",
                                 "text": f"notice {i}: " + "x" * 2000})
    got: list[str] = []
    for _ in range(10):
        text, err = T.call(me, "wait_for", {"events": ["changeset"], "timeout_s": 1}, limit=budget)
        assert not err and len(text) <= budget, (len(text), text[:200])
        out = json.loads(text)
        assert "_truncated" not in out, out
        if out["event"] is None:
            break
        got += [n["text"].split(":")[0] for n in out["notices"]]
        assert all(len(n["text"]) <= changesetmod.NOTICE_TEXT for n in out["notices"])
        if len(got) < 5:
            assert out["more_notices"] == 5 - len(got)
    assert got == [f"notice {i}" for i in range(5)]
    shutil.rmtree(root, ignore_errors=True)


def test_notices_are_given_once_the_agent_has_them(tmp_path, remotes, monkeypatch):
    """4: wait_for gives at most a few notices and says how many more wait; get_dependency_status gives none; a
    batch put back keeps the notices and who was given them; the assistant's notices are marked given only when its
    turn ends normally, so a failed turn (also after a model call answered) repeats them."""
    from http.server import HTTPServer
    from undine.agents import llm
    import test_agent as ta
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    me = _agent(srv)
    cid = _open(me)
    for i in range(7):
        cs.add_notice("p", cid, {"kind": "fetched", "op_id": f"dep-{i}", "name": "amp", "text": f"notice {i}"})
    st = _call(me, "get_dependency_status", {})
    assert st["notices_not_given"] == 7
    out = _call(me, "wait_for", {"events": ["changeset"], "timeout_s": 5})
    assert [n["text"] for n in out["notices"]] == [f"notice {i}" for i in range(5)] and out["more_notices"] == 2
    out = _call(me, "wait_for", {"events": ["changeset"], "timeout_s": 5})
    assert [n["text"] for n in out["notices"]] == ["notice 5", "notice 6"] and "more_notices" not in out
    # a batch step that fails puts the files back, not the notices nor who was given them
    def step():
        cs.add_notice("p", cid, {"kind": "fetched", "op_id": "dep-x", "name": "amp", "text": "during the step"})
        cs.mark_notices("p", cid, me.owner, 8)
        raise RuntimeError("the step fails")
    with pytest.raises(RuntimeError):
        cs.batch("p", cid, step)
    rec = cs._record("p", cid)
    assert rec["notices"][-1]["text"] == "during the step"
    assert _call(me, "wait_for", {"events": ["changeset"], "timeout_s": 1})["event"] is None   # given once only
    # the assistant: a turn that fails, even after its first model call answered, gives nothing; the next turn shows
    # the notice, ahead of the operations' lines however many they are; the one after does not repeat it
    srv.leases.release(cid, me.owner)
    httpd = HTTPServer(("127.0.0.1", 0), ta.Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    srv.gateway = llm.Gateway(root, "server")
    srv.gateway.switch("server", "m", f"http://127.0.0.1:{httpd.server_port}/v1")
    conv = {"id": "chat-n", "connection": {"kind": "server", "model": "m"}, "messages": [], "changeset": cid}
    cs.add_notice("p", cid, {"kind": "fetched", "op_id": "dep-y", "name": "amp", "text": "for the assistant"})
    real = srv.gateway.complete_tools
    calls = []

    def flaky(*a, **k):                                        # the first call answers, the second fails
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("rate limited")
        return real(*a, **k)
    monkeypatch.setattr(srv.gateway, "complete_tools", flaky)
    ta.SCRIPT[:] = [{"content": None, "tool_calls": [ta.call("get_changeset")]}]
    with pytest.raises(RuntimeError):
        agent.run(srv, "p", srv.project("p").path, conv, [], "go on", "agent:assistant", {}, owner="turn:a", turn="a")
    srv.leases.release(cid, "turn:a")
    monkeypatch.setattr(srv.gateway, "complete_tools", real)
    wctx = T.ToolCtx(srv, "p", actor="agent:w", changeset=cid)
    for i in range(70):                                        # more operations' lines than the state lists
        text, err = T.call(wctx, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1",
                                                         "params": {"value": f"{i + 2}k"}, "reason": f"step {i}"})
        assert not err, text
    ta.SEEN.clear()
    ta.SCRIPT[:] = [{"content": "ok"}]
    agent.run(srv, "p", srv.project("p").path, conv, [], "go on", "agent:assistant", {}, owner="turn:b", turn="b")
    first = json.dumps(ta.SEEN[0]["messages"][-1])
    assert "for the assistant" in first and "notice 0" in first
    srv.leases.release(cid, "turn:b")
    ta.SEEN.clear()
    ta.SCRIPT[:] = [{"content": "ok"}]
    agent.run(srv, "p", srv.project("p").path, conv, [], "more?", "agent:assistant", {}, owner="turn:c", turn="c")
    assert "for the assistant" not in json.dumps(ta.SEEN[0]["messages"][-1])
    httpd.shutdown()
    shutil.rmtree(root, ignore_errors=True)
