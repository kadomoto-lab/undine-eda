"""The scripting client (client.py): Python calls and `undine call` reach the same tools as MCP clients, keep their
session (and its changeset) between shell calls, say what to do when Undine is not usable, and reach an Undine on
another machine over SSH."""
import argparse
import io
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402

from undine.agents import mcp  # noqa: E402
from undine.server.api import create_app  # noqa: E402
from undine.agents import client as cl  # noqa: E402
from undine.agents.client import ToolError, Undine, UndineError, add_parsers, main_call, main_session, main_tools, parse_args  # noqa: E402
import test_changeset as tc  # noqa: E402
from test_mcp_local import FakeProc, transport  # noqa: E402
from undine.agents.mcp_local import Tunnel  # noqa: E402


def setup(mcp_on=True):
    root, srv, _ = tc.setup()
    srv.mcp_enabled = mcp_on
    mcp.ensure_token(srv.store.root)
    return root, srv, transport(TestClient(create_app(srv, None)))


def cli(argv, http, tmp: Path):
    os.environ["UNDINE_CLI_SESSIONS"] = str(tmp / "sessions.json")
    ap = argparse.ArgumentParser()
    add_parsers(ap.add_subparsers(dest="cmd"))
    args = ap.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    code = {"call": main_call, "tools": main_tools, "session": main_session}[args.cmd](args, http=http, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_arguments_from_the_shell():
    f = Path(__file__).resolve().parent / "test_client.py"
    assert parse_args(["cell=inv", "params:={\"W\": \"1.5\"}", "n:=3", "flag:=true", "text=@" + str(f), "reason=a:=b"]) == {
        "cell": "inv", "params": {"W": "1.5"}, "n": 3, "flag": True, "text": f.read_text(), "reason": "a:=b"}
    for bad in (["cell"], ["=x"], ["n:=not json"]):
        try:
            parse_args(bad)
            raise AssertionError(bad)
        except UndineError:
            pass


def test_python_calls_the_tools_and_keeps_its_changeset():
    root, srv, http = setup()
    with Undine("p", "http://undine.test", http=http) as u:
        names = {t["name"] for t in u.tools()}
        assert {"get_netlist", "open_changeset", "set_instance_params"} <= names
        assert u.get_netlist(lib="l", cell="top")["instances"]["R1"]["params"] == {"value": "1k"}   # parsed JSON, project from the client
        try:
            u.set_instance_params(lib="l", cell="top", instance="R1", params={"value": "10k"}, reason="x")
            raise AssertionError("an edit needs a changeset")
        except ToolError as e:
            assert "open_changeset" in str(e)
        cid = u.open_changeset(title="Raise R1", reason="less current")["id"]
        u.set_instance_params(lib="l", cell="top", instance="R1", params={"value": "10k"}, reason="less current")
        assert tc.value(srv) == "1k"                                                   # the real design is untouched
        assert tc.value(srv, f"p~{cid}") == "10k"
        again = Undine("p", "http://undine.test", session=u.session, http=http)       # the same session from elsewhere
        assert again.get_changeset()["id"] == cid
        assert u.review_url(cid) == f"http://undine.test/#/p/p?changes={cid}"
    audit = (root / "p" / "llm" / "audit.jsonl").read_text()
    assert "undine-python" in audit and "mcp:set_instance_params" in audit          # recorded like any MCP client
    shutil.rmtree(root)


def test_the_client_imports_as_the_docs_say_and_refuses_a_name_for_a_session():
    """`from undine.client import Undine` (the skill's and the guide's) is the client of undine.agents.client; session=
    is an MCP session's id, and a name given there (the shell's --session NAME) is refused, saying agent= is the name."""
    from undine import client as short
    from undine.agents import client as long_
    assert short.Undine is long_.Undine and short.ToolError is long_.ToolError
    with pytest.raises(UndineError, match=r"Undine\(agent='cdac-layout'\)"):
        Undine("p", "http://undine.test", session="cdac-layout")
    with pytest.raises(UndineError, match="32 hex digits"):
        Undine("p", "http://undine.test", session="not a name!")
    assert Undine("p", "http://undine.test", session="0" * 32).session == "0" * 32


def test_the_shell_keeps_its_session_between_calls(tmp_path):
    root, srv, http = setup()
    url = ["--url", "http://undine.test", "--project", "p"]
    code, out, err = cli(["call", "open_changeset", "title=Raise R1", "reason=less current", *url], http, tmp_path)
    assert code == 0, err
    cid = json.loads(out)["id"]
    assert json.loads(out)["review"] == f"http://undine.test/#/p/p?changes={cid}"     # where a person reviews it
    code, out, err = cli(["call", "set_instance_params", "lib=l", "cell=top", "instance=R1", 'params:={"value": "22k"}',
                          "reason=less current", *url], http, tmp_path)
    assert code == 0, err                                                              # still this shell's changeset
    assert tc.value(srv, f"p~{cid}") == "22k"
    code, out, err = cli(["call", "get_netlist", "lib=l", "cell=nothing", *url], http, tmp_path)
    assert code == 1 and err.splitlines()[-1] == "undine: get_netlist: no schematic nothing"
    code, out, _ = cli(["session", "show", "--url", "http://undine.test"], http, tmp_path)
    assert out.startswith("default: ") and "none" not in out
    code, out, _ = cli(["session", "end", "--url", "http://undine.test"], http, tmp_path)
    assert code == 0 and json.loads((tmp_path / "sessions.json").read_text()) == {}
    code, out, err = cli(["call", "set_instance_params", "lib=l", "cell=top", "instance=R1", 'params:={"value": "1M"}',
                          "reason=x", *url], http, tmp_path)
    assert code == 1 and "open_changeset" in err                                       # a new session has no changeset
    shutil.rmtree(root)


def test_the_shell_sends_the_request_id_it_is_given(tmp_path, monkeypatch):
    """A write sent again with its request_id acts once: the id given with --request-id, as an argument or in --json is
    the one sent (else a new one), and a lost answer names it."""
    root, srv, http = setup()
    sent = []

    def spy(method, path, body=None, headers=None):
        if isinstance(body, dict) and body.get("method") == "tools/call":
            sent.append(body["params"]["arguments"].get("request_id"))
        return http(method, path, body, headers)
    url = ["--url", "http://undine.test", "--project", "p"]
    ids = []
    for argv in (["title=A", "reason=r", "request_id=abc"], ["--json", '{"title": "A", "reason": "r", "request_id": "abc"}'],
                 ["title=A", "reason=r", "--request-id", "abc"]):
        code, out, err = cli(["call", "open_changeset", *argv, *url], spy, tmp_path)
        assert code == 0, err
        ids.append(json.loads(out)["id"])
    assert sent == ["abc"] * 3 and len(set(ids)) == 1                                  # opened once, answered thrice
    code, out, err = cli(["call", "list_changesets", *url], spy, tmp_path)
    assert code == 0 and sent[-1] not in ("abc", None)                                 # a new one

    def lost(method, path, body=None, headers=None):
        if isinstance(body, dict) and body.get("method") == "tools/call":
            raise OSError("timed out")
        return http(method, path, body, headers)
    monkeypatch.setattr(cl, "RETRIES", ())
    code, _, err = cli(["call", "open_changeset", "title=B", "reason=r", "request_id=xyz", *url], lost, tmp_path)
    assert code == 2 and "did not answer" in err and "--request-id xyz" in err
    shutil.rmtree(root)


def test_tools_are_listed_and_described(tmp_path):
    root, srv, http = setup()
    code, out, _ = cli(["tools", "--url", "http://undine.test"], http, tmp_path)
    assert code == 0 and any(line.startswith("get_netlist ") for line in out.splitlines())
    code, out, _ = cli(["tools", "set_instance_params", "--url", "http://undine.test"], http, tmp_path)
    assert "  params: {name: string} (required)" in out and "  project: string (required)" in out
    code, out, _ = cli(["tools", "delete_objects", "--url", "http://undine.test"], http, tmp_path)   # what goes in a list
    assert "  objects: [string or {kind: instance|wire, id: string}, ...] (required)" in out
    code, out, err = cli(["tools", "no_such_tool", "--url", "http://undine.test"], http, tmp_path)
    assert code == 1 and "undine tools" in err
    shutil.rmtree(root)


def test_says_what_to_do_when_undine_is_not_usable(tmp_path):
    root, srv, http = setup(mcp_on=False)
    code, _, err = cli(["call", "list_cells", "--url", "http://undine.test", "--project", "p"], http, tmp_path)
    assert code == 2 and "without MCP" in err
    def down(*a, **k):
        raise OSError("refused")
    code, _, err = cli(["call", "list_cells", "--url", "http://127.0.0.1:9", "--project", "p"], down, tmp_path)
    assert code == 2 and "is not running" in err and "--start" in err
    try:
        Undine(None, "http://undine.test", http=http).call("list_cells")
        raise AssertionError("a project is needed")
    except UndineError as e:
        assert "project" in str(e)
    shutil.rmtree(root)


def fake_tunnel(srv, host="lab", url="http://127.0.0.1:8700"):
    """A Tunnel whose ssh is simulated: `started` lists each forward opened, the token is read from the server's root."""
    started = []

    def popen(args, **kw):
        started.append(FakeProc())
        return started[-1]

    def run(args, **kw):
        return type("R", (), {"returncode": 0, "stdout": (srv.store.root / "mcp-token").read_text(), "stderr": ""})()
    t = Tunnel(host, url, popen=popen, run=run, probe=lambda port: True)
    t.started = started
    return t


def test_python_over_ssh_keeps_its_session_across_forwards(monkeypatch):
    root, srv, http = setup()
    t = fake_tunnel(srv)
    u = Undine("p", "http://127.0.0.1:8700", http=http, ssh=t)
    cid = u.open_changeset(title="remote", reason="r")["id"]
    assert u.where == "http://127.0.0.1:8700 on lab" and len(t.started) == 1
    # the review comes after the script: its URL is as seen on that host, not the forward's local end
    assert u.review_url(cid) == f"http://127.0.0.1:8700/#/p/p?changes={cid}"
    session = u.session
    u.disconnect()                                                   # the forward closes, the session stays
    assert t.started[0].stopped and t.proc is None and u.session == session
    monkeypatch.setattr(cl, "RETRIES", (0.0, 0.0))
    u.set_instance_params(lib="l", cell="top", instance="R1", params={"value": "2k"}, reason="x")
    assert len(t.started) == 2 and u.session == session              # opened again; still the same changeset's writer
    t.started[1].code = 255                                          # the forward dropped between calls: opened before sending
    u.set_instance_params(lib="l", cell="top", instance="R1", params={"value": "3k"}, reason="y")
    assert len(t.started) == 3
    sent = []

    def flaky(method, path, body=None, headers=None):              # it drops while a call is on its way
        sent.append(path)
        if len(sent) == 1:
            t.started[-1].code = 255
            raise OSError("connection reset")
        return http(method, path, body, headers)
    u._l.http = flaky
    u.set_instance_params(lib="l", cell="top", instance="R1", params={"value": "4k"}, reason="z")
    assert len(t.started) == 4                                       # sent again through a new forward
    assert srv.editor(f"p~{cid}", "l", "top", "schematic").instance("R1")["props"]["value"] == "4k"
    u.close()
    assert t.proc is None and srv.leases.get(cid) is None
    shutil.rmtree(root)


def test_the_shell_over_ssh_closes_the_forward_after_each_call(tmp_path, monkeypatch):
    root, srv, http = setup()
    tunnels = []

    def make(host, url):
        tunnels.append(fake_tunnel(srv, host, url))
        return tunnels[-1]
    monkeypatch.setattr(cl, "Tunnel", make)
    code, out, err = cli(["call", "open_changeset", "--ssh", "lab", "--project", "p", "title=t", "reason=r"], http, tmp_path)
    assert code == 0, err
    cid = json.loads(out)["id"]
    assert json.loads(out)["review"] == f"http://127.0.0.1:8700/#/p/p?changes={cid}"
    code, out, err = cli(["call", "set_instance_params", "--ssh", "lab", "--project", "p", "lib=l", "cell=top", "instance=R1",
                          "params:={\"value\": \"5k\"}", "reason=x"], http, tmp_path)
    assert code == 0, err                                            # the same session: the changeset is still open in it
    assert len(tunnels) == 2 and all(t.proc is None for t in tunnels) and all(t.started[0].stopped for t in tunnels)
    sessions = json.loads((tmp_path / "sessions.json").read_text())
    assert list(sessions) == ["http://127.0.0.1:8700 on lab|default"]   # kept apart from a local server's sessions
    code, out, _ = cli(["session", "--ssh", "lab"], http, tmp_path)
    assert "on lab" in out
    shutil.rmtree(root)


def test_the_shell_over_ssh_says_to_start_undine_there(tmp_path, monkeypatch):
    def down(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(cl, "Tunnel", lambda host, url: Tunnel(host, url, popen=lambda *a, **k: FakeProc(), probe=lambda p: True))
    code, _, err = cli(["call", "list_changesets", "--ssh", "lab", "--project", "p"], down, tmp_path)
    assert code == 2 and "on lab" in err and "Start it there" in err and "--start" not in err


def test_a_kept_session_the_server_forgot_starts_over(tmp_path):
    """undine call keeps its session between processes; after a server restart the server answers 404 and the client
    starts a new session under its own name, then runs the call."""
    root, srv, http = setup()
    url = ["--url", "http://undine.test", "--project", "p"]
    cli(["call", "list_cells", *url], http, tmp_path)
    s = json.loads((tmp_path / "sessions.json").read_text())
    (tmp_path / "sessions.json").write_text(json.dumps({k: "forgotten" for k in s}))   # as if the server restarted
    code, out, err = cli(["call", "open_changeset", "title=after restart", "reason=r", *url], http, tmp_path)
    assert code == 0, err
    assert [c["actor"] for c in tc.Changesets(srv).list("p") if c["title"] == "after restart"] == ["agent:undine-cli"]
    assert list(json.loads((tmp_path / "sessions.json").read_text()).values()) != ["forgotten"]
    shutil.rmtree(root)
