"""Agents side by side through the shell, and one checking another's work: two `undine call` sessions with sessions
files of their own are two writers with changesets of their own (also through the real CLI against a real server,
across a restart); a named shell session is the agent of that name (history, review, write lock), and one named
before that goes on with its changeset under its name; `changeset=` reads and checks another's open changeset without
its write lock and without making it the caller's (with the real KLayout, Magic and ngspice); job ids never repeat,
also with many jobs in one second."""
import argparse
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.agents import agent_tools as T  # noqa: E402
from undine.agents import mcp  # noqa: E402
from undine.agents.client import ToolError, Undine, add_parsers, main_call, main_session  # noqa: E402
import test_changeset as tc  # noqa: E402
from test_client import setup as client_setup  # noqa: E402

SERVER = Path(__file__).resolve().parents[1]
URL = ["--url", "http://undine.test", "--project", "p"]
R1 = ["lib=l", "cell=top", "instance=R1"]


def cli(argv, http, sessions: Path):
    """`undine call` / `undine session` in this process, with its own sessions file (UNDINE_CLI_SESSIONS)."""
    os.environ["UNDINE_CLI_SESSIONS"] = str(sessions)
    ap = argparse.ArgumentParser()
    add_parsers(ap.add_subparsers(dest="cmd"))
    args = ap.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    code = {"call": main_call, "session": main_session}[args.cmd](args, http=http, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ two shell sessions


def test_two_shell_sessions_with_files_of_their_own_are_two_writers(tmp_path):
    """Designer A and designer B, each with its own sessions file and --session, no agent=: B gets a changeset of its
    own, list_changesets marks only its own as yours, and each answer says who the caller is, so that calls made in
    another's session (a helper script two agents share) show at once."""
    root, srv, http = client_setup()
    fa, fb = tmp_path / "a.json", tmp_path / "b.json"
    code, out, err = cli(["call", "open_changeset", "title=A", "reason=r", "--session", "bootsw-a", *URL], http, fa)
    assert code == 0, err
    a = json.loads(out)
    assert a["you"].startswith("undine-cli/bootsw-a (MCP session ")
    code, out, err = cli(["call", "list_changesets", "--session", "bootsw-b", *URL], http, fb)
    rows = json.loads(out)
    assert [r for r in rows["open"] if r.get("yours")] == [] and rows["you"].startswith("undine-cli/bootsw-b")
    assert not rows["open"][0]["writer"]["you"] and "undine-cli/bootsw-a" in rows["open"][0]["writer"]["who"]
    code, out, err = cli(["call", "open_changeset", "title=B", "reason=r", "--session", "bootsw-b", *URL], http, fb)
    b = json.loads(out)
    assert code == 0 and b["id"] != a["id"] and "already open" not in b["note"]
    for f, name, mine, theirs in ((fa, "bootsw-a", a["id"], b["id"]), (fb, "bootsw-b", b["id"], a["id"])):
        code, out, err = cli(["call", "set_instance_params", *R1, f'params:={{"value": "{name}"}}', "reason=r",
                              "--session", name, *URL], http, f)
        assert code == 0 and err.startswith(f"undine: request id ") and f"(agent {name})" in err, err
        rows = {r["id"]: r for r in json.loads(cli(["call", "list_changesets", "--session", name, *URL], http, f)[1])["open"]}
        assert rows[mine].get("yours") and rows[mine]["writer"]["you"]
        assert not rows[theirs].get("yours") and not rows[theirs]["writer"]["you"]
    assert (tc.value(srv, f"p~{a['id']}"), tc.value(srv, f"p~{b['id']}")) == ("bootsw-a", "bootsw-b")
    # one file can hold both sessions too (by name), and a script that runs in the other's session says whose it is
    code, out, err = cli(["call", "open_changeset", "title=B again", "reason=r", "--session", "bootsw-a", *URL], http, fb)
    again = json.loads(out)
    assert again["id"] != a["id"] and again["you"].startswith("undine-cli/bootsw-a")
    code, out, err = cli(["call", "open_changeset", "title=A again", "reason=r", "--session", "bootsw-a", *URL], http, fa)
    same = json.loads(out)
    assert same["id"] == a["id"] and "already open in this session (undine-cli/bootsw-a" in same["note"]
    assert "opened by agent:undine-cli/bootsw-a" in same["note"]
    audit = [json.loads(x) for x in (root / "p" / "llm" / "audit.jsonl").read_text().splitlines()]
    sids = {e["payload"]["session"] for e in audit if e.get("purpose") == "mcp:open_changeset"}
    assert len(sids) == 3                                  # which session each call ran in is in the audit log
    shutil.rmtree(root)


# ------------------------------------------------------------------ a named shell session is that agent


def test_a_named_shell_session_is_the_agent_of_that_name(tmp_path):
    root, srv, http = client_setup()
    f = tmp_path / "s.json"
    code, out, err = cli(["call", "open_changeset", "title=A", "reason=r", "--session", "layout-a", *URL], http, f)
    cid = json.loads(out)["id"]
    assert tc.Changesets(srv).get("p", cid)["actor"] == "agent:undine-cli/layout-a"
    code, out, err = cli(["call", "set_instance_params", *R1, 'params:={"value": "2k"}', "reason=r",
                          "--session", "layout-a", *URL], http, f)
    le = srv.leases.get(cid)
    assert le.owner.endswith(":layout-a") and le.label == "undine-cli/layout-a"          # the review names it
    assert {r["actor"] for r in srv.oplog(f"p~{cid}", "l").read() if r.get("changeset") == cid} == {"agent:undine-cli/layout-a"}
    kept = json.loads(f.read_text())["http://undine.test|layout-a"]
    assert kept["changesets"] == {"p|layout-a": cid}
    code, out, _ = cli(["session", "show", "--session", "layout-a", "--url", "http://undine.test"], http, f)
    assert "agent layout-a" in out and str(f) in out
    # agent= given wins: another agent on the same session, with a changeset of its own
    code, out, err = cli(["call", "open_changeset", "title=B", "reason=r", "agent=helper", "--session", "layout-a",
                          *URL], http, f)
    assert json.loads(out)["id"] != cid and "(agent helper)" in err
    # the default session and a name that is no agent name stay unnamed
    code, out, err = cli(["call", "open_changeset", "title=C", "reason=r", *URL], http, f)
    assert tc.Changesets(srv).get("p", json.loads(out)["id"])["actor"] == "agent:undine-cli" and "(agent" not in err
    code, out, err = cli(["call", "open_changeset", "title=D", "reason=r", "--session", "a b", *URL], http, f)
    assert tc.Changesets(srv).get("p", json.loads(out)["id"])["actor"] == "agent:undine-cli"
    shutil.rmtree(root)


def test_a_new_named_session_keeps_its_unnamed_agents_changeset_apart(tmp_path):
    """A session this client made: a changeset its unnamed agent opens (agent= given) stays that agent's; the session's
    own agent never takes it over, also after the server forgot the session."""
    root, srv, http = client_setup()
    f = tmp_path / "s.json"
    key = "http://undine.test|x"
    code, out, err = cli(["call", "open_changeset", "title=U", "reason=r", "agent=", "--session", "x", *URL], http, f)
    unnamed = json.loads(out)["id"]
    assert code == 0 and json.loads(f.read_text())[key]["named"] is True
    code, out, err = cli(["call", "list_changesets", "--session", "x", *URL], http, f)
    assert not any(r.get("yours") for r in json.loads(out)["open"])          # the named agent has none
    code, out, err = cli(["call", "set_instance_params", *R1, 'params:={"value": "2k"}', "reason=r", "agent=",
                          "--session", "x", *URL], http, f)
    assert code == 0 and tc.value(srv, f"p~{unnamed}") == "2k", err
    assert tc.Changesets(srv).get("p", unnamed)["actor"] == "agent:undine-cli"
    code, out, err = cli(["call", "set_instance_params", *R1, 'params:={"value": "3k"}', "reason=r",
                          "--session", "x", *URL], http, f)
    assert code == 1 and "no changeset is open" in err
    # the server forgot the session: a new one goes on with the unnamed agent's changeset as the unnamed agent's
    kept = json.loads(f.read_text())
    srv.leases.release_owner("mcp:" + kept[key]["session"])
    kept[key]["session"] = "0" * 32
    f.write_text(json.dumps(kept))
    code, out, err = cli(["call", "set_instance_params", *R1, 'params:={"value": "4k"}', "reason=r", "agent=",
                          "--session", "x", *URL], http, f)
    assert code == 0 and tc.value(srv, f"p~{unnamed}") == "4k", err
    assert json.loads(f.read_text())[key]["changesets"] == {"p|": unnamed}
    shutil.rmtree(root)


def test_a_session_named_before_goes_on_with_its_changeset_under_its_name(tmp_path):
    """A --session that opened its changeset before its name was its agent (the session's unnamed agent holds it, its
    write lock too): its next call goes on in that changeset as the named agent at once, the lock handed over; a
    session the server forgot meanwhile continues it under the name as well; a stopped one stays stopped."""
    root, srv, http = client_setup()
    f = tmp_path / "s.json"
    key = "http://undine.test|old"

    def before(write: bool = True):
        """What a client before this left: a session whose unnamed agent opened (and wrote) the changeset."""
        u = Undine("p", "http://undine.test", http=http, client="undine-cli")
        cid = u.open_changeset(title="old", reason="r")["id"]
        if write:
            u.set_instance_params(lib="l", cell="top", instance="R1", params={"value": "3k"}, reason="r")
        f.write_text(json.dumps({key: {"session": u.session, "changesets": {"p|": cid}}}))
        return u, cid

    u, cid = before()
    code, out, err = cli(["call", "set_instance_params", *R1, 'params:={"value": "5k"}', "reason=r",
                          "--session", "old", *URL], http, f)
    assert code == 0, err                                   # at once: the lock is the name's now
    assert tc.value(srv, f"p~{cid}") == "5k"
    assert srv.leases.get(cid).owner == f"mcp:{u.session}:old" and srv.leases.get(cid).label == "undine-cli/old"
    assert json.loads(f.read_text())[key] == {"session": u.session, "changesets": {"p|old": cid}, "named": True}
    with pytest.raises(ToolError, match="no changeset is open"):          # the unnamed agent let it go
        u.set_instance_params(lib="l", cell="top", instance="R1", params={"value": "4k"}, reason="r")
    assert json.loads(cli(["call", "get_changeset", "--session", "old", *URL], http, f)[1])["id"] == cid
    # once only: a changeset the session's unnamed agent opens now (agent= given) stays the unnamed agent's
    code, out, err = cli(["call", "open_changeset", "title=U", "reason=r", "agent=", "--session", "old", *URL], http, f)
    unnamed = json.loads(out)["id"]
    assert unnamed != cid
    assert json.loads(cli(["call", "get_changeset", "--session", "old", *URL], http, f)[1])["id"] == cid
    code, out, err = cli(["call", "set_instance_params", *R1, 'params:={"value": "8k"}', "reason=r", "agent=",
                          "--session", "old", *URL], http, f)
    assert code == 0 and tc.value(srv, f"p~{unnamed}") == "8k" and tc.value(srv, f"p~{cid}") == "5k", err
    assert json.loads(f.read_text())[key]["changesets"] == {"p|old": cid, "p|": unnamed}
    # the server forgot the session: a new one continues the changeset under the name
    u, cid = before()
    u.close()
    code, out, err = cli(["call", "set_instance_params", *R1, 'params:={"value": "6k"}', "reason=r",
                          "--session", "old", *URL], http, f)
    assert code == 0 and tc.value(srv, f"p~{cid}") == "6k", err
    assert srv.leases.get(cid).owner.endswith(":old")
    assert json.loads(f.read_text())[key]["changesets"] == {"p|old": cid}
    # stopped before the hand over: stopped after it
    u, cid = before()
    srv.leases.stop(cid)
    code, out, err = cli(["call", "set_instance_params", *R1, 'params:={"value": "7k"}', "reason=r",
                          "--session", "old", *URL], http, f)
    assert code == 1 and "was stopped" in err and tc.value(srv, f"p~{cid}") == "3k"
    assert f"mcp:{u.session}:old" in json.loads((srv.store.root / mcp.SESSIONS).read_text())["stopped"]
    shutil.rmtree(root)


# ------------------------------------------------------------------ the real CLI against a real server


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(root: Path, port: int, log: Path) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-m", "undine", "--root", str(root), "serve", "--port", str(port), "--mcp"],
                         cwd=SERVER, stdout=log.open("a"), stderr=subprocess.STDOUT,
                         env={**os.environ, "PYTHONPATH": str(SERVER), "PYTHONUNBUFFERED": "1"})
    t0 = time.time()
    while time.time() - t0 < 120:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.5):
                return p
        except OSError:
            assert p.poll() is None, log.read_text()
            time.sleep(0.2)
    p.kill()
    raise AssertionError(log.read_text())


def _stop(p: subprocess.Popen) -> None:
    p.terminate()
    try:
        p.wait(30)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def test_two_shell_sessions_through_the_real_cli_and_a_restart(tmp_path):
    """`python -m undine call` processes against `undine serve --mcp`: two designers with sessions files of their own
    and no agent= get changesets of their own, named after their sessions, also after the server restarts."""
    from undine.schematics import xschem
    from undine.store.project import ProjectStore

    root, log, port = tmp_path / "root", tmp_path / "serve.log", _port()
    lib = ProjectStore(root, {}).create("p").create_library("l")
    (lib.path / "r.sym").write_text(tc.R_SYM)
    sch = xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n")
    sch.comps = [xschem.XComp("r.sym", 0, 0, 0, 0, {"name": "R1", "value": "1k"})]
    (lib.path / "top.sch").write_text(xschem.dump(sch))

    def call(who: str, *args: str):
        env = {**os.environ, "PYTHONPATH": str(SERVER), "UNDINE_CLI_SESSIONS": str(tmp_path / f"{who}.json"),
               "UNDINE_URL": f"http://127.0.0.1:{port}"}
        r = subprocess.run([sys.executable, "-m", "undine", "call", *args, "--project", "p", "--session", who, "--raw"],
                           cwd=SERVER, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout), r.stderr

    srv = _serve(root, port, log)
    try:
        a, err = call("bootsw-a", "open_changeset", "title=A", "reason=r")
        assert "(agent bootsw-a)" in err
        rows, _ = call("bootsw-b", "list_changesets")
        assert not any(r.get("yours") for r in rows["open"])
        b, _ = call("bootsw-b", "open_changeset", "title=B", "reason=r")
        assert b["id"] != a["id"]
        _stop(srv)
        srv = _serve(root, port, log)                       # a restart: both sessions go on with their own
        for who, mine in (("bootsw-a", a["id"]), ("bootsw-b", b["id"])):
            got, _ = call(who, "open_changeset", "title=again", "reason=r")
            assert got["id"] == mine and got["you"].startswith(f"undine-cli/{who} ")
            call(who, "set_instance_params", *R1, f'params:={{"value": "{who}"}}', "reason=r")
            rows, _ = call(who, "list_changesets")
            assert [r["id"] for r in rows["open"] if r.get("yours")] == [mine]
        for who, cid in (("bootsw-a", a["id"]), ("bootsw-b", b["id"])):
            meta = json.loads((root / "p" / "changesets" / f"{cid}.json").read_text())
            assert meta["actor"] == f"agent:undine-cli/{who}"
    finally:
        _stop(srv)


# ------------------------------------------------------------------ reading and checking another's changeset


def test_a_verifier_reads_and_checks_another_changeset_without_taking_it():
    """The writer holds its changeset's lock; a verifier (another session, with no changeset of its own) runs the
    reads and checks on it with changeset=: KLayout and Magic DRC, LVS, extraction and an ngspice run, recorded in that
    changeset as the writer's are, the jobs it started marked its own (it may stop them), the verifier's session left
    without a changeset, the writer's lock, operations and branch untouched, and the writer writing on meanwhile. A
    write tool does not take changeset."""
    import test_parasitics as tp

    pdk = tp._need(sim=True)
    if not tools_ok():
        pytest.skip("KLayout is not installed")
    root, srv, _ = tp._inverter(pdk, wire_um=50.0)
    writer = T.ToolCtx(srv, "p", actor="agent:claude-code/layout", owner="mcp:" + "a" * 32 + ":layout", owner_kind="mcp")
    verifier = T.ToolCtx(srv, "p", actor="agent:claude-code/verify", owner="mcp:" + "b" * 32 + ":verify",
                         owner_kind="mcp")
    cid = tp._call(writer, "open_changeset", {"title": "a wider out wire", "reason": "r"})["id"]
    tp._call(writer, "add_box", {"lib": "l", "cell": "invw", "layer": "met1", "box": [-3.0, 2.0, -1.3, 2.4],
                                 "reason": "wider"})
    cs = tc.Changesets(srv)
    ops = len(cs.get("p", cid)["changes"][0]["ops"])
    tip = cs._tip_state("p", cid)
    assert srv.leases.get(cid).owner == writer.owner
    info = tp._call(verifier, "get_layout_info", {"lib": "l", "cell": "invw", "changeset": cid})
    assert info["changeset"] == cid and verifier.changeset is None
    res, err = T.call(verifier, "render_layout", {"lib": "l", "cell": "invw", "changeset": cid})
    assert not err and verifier.images and json.loads(res)["changeset"] == cid
    checks = tp._call(verifier, "run_checks", {"changeset": cid})
    rows = [r for r in checks["checks"] if r["side"] == "work" and r["kind"] in ("drc", "lvs")]
    assert {(r["kind"], r["checker"]) for r in rows} >= {("drc", "klayout"), ("drc", "magic"), ("lvs", "klayout")}
    assert all(r["state"] == "done" and r.get("job") for r in rows), rows
    work = srv.project(f"p~{cid}").path
    assert all((work / "runs" / r["job"]).is_dir() for r in rows)                  # in its working copy
    assert {r["job"] for r in cs.get("p", cid)["verify"] if r["side"] == "work"} >= {r["job"] for r in rows}
    drc = tp._call(verifier, "get_drc_report", {"lib": "l", "cell": "invw", "changeset": cid})
    assert drc["changeset"] == cid and drc["runs"]
    ex = tp._call(verifier, "extract_parasitics", {"lib": "l", "cell": "invw", "mode": "c", "changeset": cid})
    assert (work / "runs" / ex["job"] / "parasitics.json").is_file()
    # the jobs it started there are its own (a reader's): it may stop them, and only them
    for jid in (ex["job"], rows[0]["job"]):
        st = srv.jobs.get(work, jid).status()
        assert st["owner"] == verifier.owner and st["reader"] is True, st
        res, err = T.call(verifier, "cancel_job", {"job": jid, "changeset": cid})
        assert not err and "not running" in res, res
    assert tp._call(verifier, "get_parasitics", {"lib": "l", "cell": "invw", "changeset": cid})["job"] == ex["job"]
    run = tp._call(verifier, "run_simulation", {"lib": "l", "cell": "tb", "analysis": tp.TRAN, "changeset": cid,
                                                "extracted": ["l/invw"]})
    st = srv.jobs.get(work, run["job"]).status()
    assert st["owner"] == verifier.owner and st["reader"] is True and run["extracted"][0]["job"] == ex["job"]
    # nothing of the changeset is the verifier's, and nothing of it changed but what the checks recorded
    assert verifier.changeset is None and srv.leases.get(cid).owner == writer.owner
    assert len(cs.get("p", cid)["changes"][0]["ops"]) == ops and cs._tip_state("p", cid) == tip
    # the writer writes on meanwhile; the verifier writes nothing there
    tp._call(writer, "add_box", {"lib": "l", "cell": "invw", "layer": "met1", "box": [-3.0, 2.5, -1.3, 2.6],
                                 "reason": "again"})
    res, err = T.call(verifier, "add_box", {"lib": "l", "cell": "invw", "layer": "met1", "box": [0, 0, 1, 1],
                                            "reason": "x", "changeset": cid})
    assert err and "does not take changeset" in res
    res, err = T.call(writer, "cancel_job", {"job": run["job"]})
    assert not err or "not running" in res                                    # the writer may stop jobs in its own
    res, err = T.call(verifier, "get_layout_info", {"lib": "l", "cell": "invw", "changeset": "cs-000000"})
    assert err and "no changeset cs-000000" in res
    # its own changeset = its own: no reader
    own = tp._call(writer, "get_layout_info", {"lib": "l", "cell": "invw", "changeset": cid})
    assert "changeset" not in own
    shutil.rmtree(root)


def test_a_verifier_checks_a_layout_its_writer_never_saved():
    """A library and a cell the writer made and drew, never saved nor committed (the writer has not run its checks):
    the verifier's run_checks finds the layout as it is in memory, runs KLayout's and Magic's DRC on it, and commits
    nothing of the changeset."""
    import test_layout_checks as tl

    pdk = tl._need()
    root, srv = tl._server(pdk, {})
    writer = T.ToolCtx(srv, "p", actor="agent:w", owner="mcp:" + "a" * 32, owner_kind="mcp")
    verifier = T.ToolCtx(srv, "p", actor="agent:v", owner="mcp:" + "b" * 32, owner_kind="mcp")
    cid = tl._call(writer, "open_changeset", {"title": "narrow", "reason": "r"})["id"]
    tl._call(writer, "create_library", {"name": "lw", "reason": "r"})
    tl._call(writer, "create_cell", {"lib": "lw", "cell": "blk", "views": ["layout"], "reason": "r"})
    tl._call(writer, "add_box", {"lib": "lw", "cell": "blk", "layer": "met1", "box": [0, 0, 1, 0.1], "reason": "narrow"})
    rows = tl._call(verifier, "run_checks", {"changeset": cid})["checks"]
    assert {(r["kind"], r["checker"], r["state"], r["ok"]) for r in rows} == {("drc", "klayout", "done", False),
                                                                           ("drc", "magic", "done", False)}, rows
    assert tc.Changesets(srv)._tip_state("p", cid).get("commits", 0) == 0 and srv.leases.get(cid).owner == writer.owner
    shutil.rmtree(root)


def tools_ok() -> bool:
    from undine import tools
    return bool(tools.find("klayout"))


def test_a_verifier_stops_only_its_own_jobs_in_another_changeset(tmp_path):
    root, srv, http = client_setup()
    writer = T.ToolCtx(srv, "p", actor="agent:w", owner="mcp:" + "a" * 32, owner_kind="mcp")
    verifier = T.ToolCtx(srv, "p", actor="agent:v", owner="mcp:" + "b" * 32, owner_kind="mcp")
    res, err = T.call(writer, "open_changeset", {"title": "w", "reason": "r"})
    cid = json.loads(res)["id"]
    work = srv.project(f"p~{cid}").path
    for jid, owner in (("20261104-120000-aaaa", writer.owner), ("20261104-120000-bbbb", verifier.owner)):
        d = work / "runs" / jid
        d.mkdir(parents=True)
        (d / "status.json").write_text(json.dumps({"id": jid, "kind": "sim", "state": "done", "owner": owner,
                                                   "created": time.time()}))
    res, err = T.call(verifier, "cancel_job", {"job": "20261104-120000-aaaa", "changeset": cid})
    assert err and "not one of yours" in res
    res, err = T.call(verifier, "cancel_job", {"job": "20261104-120000-bbbb", "changeset": cid})
    assert not err and "not running" in res
    res, err = T.call(verifier, "set_instance_params", {**dict(x.split("=") for x in R1), "params": {"value": "2"},
                                                        "reason": "x", "changeset": cid})
    assert err and "does not take changeset" in res and tc.value(srv, f"p~{cid}") == "1k"
    # a reader's job seeds no session at the first restart from an older Undine (mcp.Sessions._seed)
    (work / "runs" / "20261104-120000-bbbb" / "status.json").write_text(json.dumps(
        {"id": "x", "kind": "sim", "state": "running", "owner": verifier.owner, "reader": True, "created": time.time()}))
    (srv.store.root / mcp.SESSIONS).unlink(missing_ok=True)
    s = mcp.Sessions(srv)
    assert "b" * 32 not in s.s
    shutil.rmtree(root)


def _pair(srv):
    writer = T.ToolCtx(srv, "p", actor="agent:undine-cli/w", owner="mcp:" + "a" * 32 + ":w", owner_kind="mcp")
    verifier = T.ToolCtx(srv, "p", actor="agent:undine-cli/v", owner="mcp:" + "b" * 32 + ":v", owner_kind="mcp")
    return writer, verifier


def _ok(ctx, tool, args):
    res, err = T.call(ctx, tool, args, None)
    assert not err, res
    try:
        return json.loads(res)
    except ValueError:
        return res


def test_a_verifier_that_once_opened_the_changeset_still_only_reads_it():
    """A verifier whose session has the writer's changeset as its own (it ran open_changeset id=, the old way to look
    at it): changeset= reads and checks it all the same, while the writer writes (not refused) and once its lock has
    lapsed (no lock taken, nothing of it committed under the verifier's name); the writer's own changeset= is its own."""
    root, srv, cs = tc.setup()
    writer, verifier = _pair(srv)
    cid = _ok(writer, "open_changeset", {"title": "w", "reason": "r"})["id"]
    _ok(writer, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "9k"},
                                        "reason": "r"})
    opened = _ok(verifier, "open_changeset", {"id": cid})
    assert opened["writer"] and verifier.changeset == cid
    for lapsed in (False, True):
        if lapsed:
            srv.leases.release(cid, writer.owner)
        tip = cs._tip_state("p", cid)
        net = _ok(verifier, "get_netlist", {"lib": "l", "cell": "top", "changeset": cid})
        assert net["changeset"] == cid and "9k" in json.dumps(net)
        checks = _ok(verifier, "run_checks", {"changeset": cid})
        assert checks["changeset"] == cid
        assert cs._tip_state("p", cid) == tip                             # nothing of it committed
        le = srv.leases.get(cid)
        assert (le.owner if le else None) == (None if lapsed else writer.owner)
    assert verifier.changeset == cid
    _ok(writer, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "10k"},
                                        "reason": "r"})
    assert "changeset" not in _ok(writer, "get_netlist", {"lib": "l", "cell": "top", "changeset": cid})
    shutil.rmtree(root)


def test_a_reader_is_told_how_to_go_on_in_the_changeset_it_reads():
    """What a reader is told to call next names the changeset it reads (changeset=), and a job looked for outside it
    says to pass it, not to open the changeset; a text answer starts with the changeset too; a batch op does not take
    changeset (it would read the caller's own); the hint for one who finds another's changeset as its own fits its
    client."""
    root, srv, cs = tc.setup()
    writer, verifier = _pair(srv)
    cid = _ok(writer, "open_changeset", {"title": "w", "reason": "r"})["id"]
    _ok(writer, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "9k"},
                                        "reason": "r"})
    work = srv.project(f"p~{cid}").path
    jid = "20261104-120000-0000"
    (work / "runs" / jid).mkdir(parents=True)
    (work / "runs" / jid / "status.json").write_text(json.dumps(
        {"id": jid, "kind": "sim", "state": "done", "lib": "l", "cell": "top", "created": time.time()}))
    res, err = T.call(verifier, "cancel_job", {"job": jid})
    assert err and f"pass changeset={cid} to read it there" in res and "open_changeset" in res
    _ok(verifier, "open_changeset", {"title": "mine", "reason": "r"})
    res, err = T.call(verifier, "cancel_job", {"job": jid})
    assert err and f"pass changeset={cid}" in res and "open_changeset" not in res
    got = _ok(verifier, "wait_for", {"events": [f"job:{jid}"], "changeset": cid})
    assert f"job='{jid}', changeset='{cid}'" in got["next"]
    for tool in ("get_design_context", "get_history"):
        text = _ok(verifier, tool, {"lib": "l", "cell": "top", "changeset": cid})
        assert isinstance(text, str) and text.startswith(f"changeset: {cid}\n"), text
    # a batch reads the caller's own changeset: an op that asks for another's is refused
    res, err = T.call(verifier, "batch", {"ops": [{"tool": "get_netlist", "args": {"lib": "l", "cell": "top",
                                                                                   "changeset": cid}}]})
    assert err and "ops[0]: changeset reads another's changeset" in res
    assert "9k" in json.dumps(_ok(verifier, "get_netlist", {"lib": "l", "cell": "top", "changeset": cid}))
    # "already open": a shell agent is told of --session, another MCP client of agent, the assistant of neither
    note = _ok(writer, "open_changeset", {"title": "again"})["note"]
    assert "already open" in note and "--session" in note
    desk = T.ToolCtx(srv, "p", actor="agent:claude-desktop", owner="mcp:" + "c" * 32, owner_kind="mcp")
    _ok(desk, "open_changeset", {"title": "d", "reason": "r"})
    note = _ok(desk, "open_changeset", {"title": "again"})["note"]
    assert "give your own name (agent)" in note and "--session" not in note
    me = T.ToolCtx(srv, "p", actor="agent:assistant", owner="turn:t-1", owner_kind="turn")
    _ok(me, "open_changeset", {"title": "a", "reason": "r"})
    note = _ok(me, "open_changeset", {"title": "again"})["note"]
    assert "already open" in note and "If that is not you" not in note
    shutil.rmtree(root)


def test_a_verifier_reads_device_geometry_in_another_changeset():
    """A device the writer placed and bound by sync_layout, in the writer's changeset only: the verifier's
    get_device_geometry with changeset= gives the writer's terminals."""
    from undine.pdks import pcell
    from undine.pdks.pdk import default_pdk_roots, discover
    from undine.server.api import Server
    import tempfile

    pdks = discover(default_pdk_roots())
    pdk = next((p for p in pdks if p.startswith("sky130A") and pcell.available(pdks[p])), None)
    if pdk is None:
        pytest.skip("sky130A with its PCell environment is not installed")
    root = Path(tempfile.mkdtemp())
    srv = Server(root, pdks, [root])
    srv.store.create("p", pdk).create_library("l")
    writer, verifier = _pair(srv)
    cid = _ok(writer, "open_changeset", {"title": "a device", "reason": "r"})["id"]
    call = lambda n, a: _ok(writer, n, {"lib": "l", "cell": "dev", "reason": "x", **a})
    call("create_cell", {"views": ["schematic", "layout"]})
    call("add_instance", {"symbol": "sky130_fd_pr/nfet_01v8.sym", "x": 0, "y": 0,
                          "params": {"name": "M1", "W": "2", "L": "0.5"}})
    call("connect_nets", {"nets": {"nd": ["M1:D"], "ng": ["M1:G"], "ns": ["M1:S"], "nb": ["M1:B"]}})
    assert call("sync_layout", {})["added"] == ["M1"]
    mine = _ok(writer, "get_device_geometry", {"lib": "l", "cell": "dev", "instance": "M1"})
    theirs = _ok(verifier, "get_device_geometry", {"lib": "l", "cell": "dev", "instance": "M1", "changeset": cid})
    assert theirs.pop("changeset") == cid and theirs == mine and mine["terminals"], mine
    res, err = T.call(verifier, "get_device_geometry", {"lib": "l", "cell": "dev", "instance": "M1"})
    assert err                                                   # not in the project: only in the changeset
    shutil.rmtree(root)
