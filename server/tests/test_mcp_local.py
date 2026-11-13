"""The local MCP entry (mcp_local.py): relays to the server, opens the review once per changeset, resumes a changeset,
tells the client what to do when Undine is not usable, and reaches a server on another machine over SSH."""
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402

from undine.agents import mcp  # noqa: E402
from undine.server.api import create_app  # noqa: E402
from undine.agents.mcp_local import Launcher, Tunnel  # noqa: E402
import test_changeset as tc  # noqa: E402


def transport(c):
    def http(method, path, body=None, headers=None):
        r = c.request(method, path, json=body, headers=headers or {})
        return r.status_code, (r.json() if r.content else None), dict(r.headers)
    return http


def rpc(lines):
    return "".join(json.dumps(x) + "\n" for x in lines)


def setup(mcp_on=True):
    root, srv, cs = tc.setup()
    srv.mcp_enabled = mcp_on
    mcp.ensure_token(srv.store.root)
    opened = []
    la = Launcher("http://undine.test", ui_url="http://ui.test", start=False, opener=opened.append,
                  http=transport(TestClient(create_app(srv, None))))
    return root, srv, la, opened


def call(i, name, **args):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": {"project": "p", **args}}}


def run(la, msgs):
    out = io.StringIO()
    la.serve_stdio(io.StringIO(rpc(msgs)), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_relays_and_opens_the_review_once_per_changeset():
    root, srv, la, opened = setup()
    res = run(la, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "desk"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        call(3, "open_changeset", title="New OTA", reason="r"),
        call(4, "set_instance_params", lib="l", cell="top", instance="R1", params={"value": "3k"}, reason="x"),
    ])
    assert [r["id"] for r in res] == [1, 2, 3, 4]                       # the notification gets no answer
    names = [t["name"] for t in res[1]["result"]["tools"]]
    assert "open_in_undine" in names and "list_changesets" in names and "create_cell" in names
    cid = json.loads(res[2]["result"]["content"][0]["text"])["id"]
    assert opened == [f"http://ui.test/#/p/p?changes={cid}"]
    assert opened[0] in res[2]["result"]["content"][1]["text"]            # the client is told where to look
    assert not res[3]["result"]["isError"]
    assert srv.editor(f"p~{cid}", "l", "top", "schematic").instance("R1")["props"]["value"] == "3k"
    # the chat ended (stdin closed): the session is over and its lock released at once
    assert la.session is None and srv.leases.get(cid) is None
    # a new session (the chat was restarted) continues the same changeset; the review is not opened again by this launcher
    res = run(la, [{"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": {}},
                   call(6, "list_changesets"), call(7, "open_changeset", id=cid),
                   call(8, "open_in_undine", changeset=cid)])
    assert cid in res[1]["result"]["content"][0]["text"]
    assert json.loads(res[2]["result"]["content"][0]["text"])["id"] == cid
    assert opened == [f"http://ui.test/#/p/p?changes={cid}"] * 2          # only the explicit open_in_undine
    # a session that is still running keeps the changeset to itself
    other = Launcher("http://undine.test", start=False, opener=opened.append, http=la.http)
    other.ensure_server()
    holder = Launcher("http://undine.test", start=False, opener=opened.append, http=la.http)
    holder.ensure_server()
    holder.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    holder.handle(call(2, "open_changeset", id=cid))
    holder.handle(call(3, "set_instance_params", lib="l", cell="top", instance="R1", params={"value": "4k"}, reason="x"))
    other.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    other.handle(call(2, "open_changeset", id=cid))
    r = other.handle(call(3, "set_instance_params", lib="l", cell="top", instance="R1", params={"value": "5k"}, reason="y"))
    assert r["result"]["isError"] and "external agent" in r["result"]["content"][0]["text"]
    holder.close()
    r = other.handle(call(4, "set_instance_params", lib="l", cell="top", instance="R1", params={"value": "5k"}, reason="y"))
    assert not r["result"]["isError"]
    shutil.rmtree(root)


def test_says_what_to_do_when_undine_has_no_mcp():
    root, srv, la, opened = setup(mcp_on=False)
    res = run(la, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                   {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, call(3, "list_changesets")])
    assert "UNDINE_MCP=1" in res[0]["result"]["instructions"]
    assert [t["name"] for t in res[1]["result"]["tools"]] == ["open_in_undine"]
    assert res[2]["result"]["isError"] and "without MCP" in res[2]["result"]["content"][0]["text"]
    shutil.rmtree(root)


def test_says_what_to_do_when_undine_is_not_running():
    def down(*a, **k):
        raise OSError("connection refused")
    la = Launcher("http://127.0.0.1:9", start=False, opener=lambda u: None, http=down)
    res = run(la, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_changesets", "arguments": {"project": "p"}}}])
    assert res[0]["result"]["isError"] and "not running" in res[0]["result"]["content"][0]["text"]


def test_a_server_it_starts_takes_its_tools_from_this_environment(tmp_path, monkeypatch):
    """The client runs the environment's Python without `pixi run`: the server it starts must still find the
    environment's tools (UNDINE_TOOLS = sys.prefix, SPICE_LIB_DIR, as pixi's activation sets them), not an older
    ~/undine-tools."""
    from undine.agents import mcp_local
    started: list[dict] = []
    up = {"undine": True, "root": str(tmp_path), "mcp": True}
    (tmp_path / "mcp-token").write_text("tok\n")
    monkeypatch.setenv("UNDINE_LOG", str(tmp_path / "serve.log"))
    monkeypatch.setenv("UNDINE_ENV", str(tmp_path / "no-env"))
    monkeypatch.delenv("UNDINE_TOOLS", raising=False)
    monkeypatch.setattr(mcp_local.subprocess, "Popen", lambda args, **kw: started.append(kw["env"]))
    monkeypatch.setattr(mcp_local.time, "sleep", lambda s: None)
    env = tmp_path / "pixi-env"
    for d in ("conda-meta", "bin", "share/ngspice"):
        (env / d).mkdir(parents=True)
    (env / "bin" / "ngspice").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "prefix", str(env))
    monkeypatch.setattr(sys, "base_prefix", str(env))
    monkeypatch.delenv("SPICE_LIB_DIR", raising=False)
    la = Launcher("http://127.0.0.1:9", opener=lambda u: None, http=lambda *a: (200, up, {}) if started else (502, None, {}))
    assert la.ensure_server() and la.token == "tok" and len(started) == 1
    assert started[0]["UNDINE_TOOLS"] == str(env) and started[0]["UNDINE_MCP"] == "1"
    assert started[0]["SPICE_LIB_DIR"] == str(env / "share" / "ngspice")
    (tmp_path / "no-env").write_text("UNDINE_TOOLS=/opt/tools\n")      # the user's own choice (~/.undine/env) stays
    assert mcp_local.server_env({**os.environ, **mcp_local._env_file()}) == {}


def test_a_server_it_starts_from_a_venv_or_a_system_python_still_finds_undine_tools(tmp_path, monkeypatch):
    """Only an environment that holds the tools becomes the tool prefix: a plain virtual environment's (or a system
    Python's) server keeps ~/undine-tools, as a server started any other way does."""
    from undine import tools
    from undine.agents import mcp_local
    venv, home = tmp_path / "venv", tmp_path / "home"
    (venv / "bin").mkdir(parents=True)
    (home / "undine-tools" / "bin").mkdir(parents=True)
    (home / "undine-tools" / "bin" / "ngspice").write_text("#!/bin/sh\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("UNDINE_TOOLS", raising=False)
    monkeypatch.setattr(sys, "prefix", str(venv))
    for base in (str(tmp_path / "python"), str(venv)):          # a venv, then a system interpreter (prefix = base)
        monkeypatch.setattr(sys, "base_prefix", base)
        assert mcp_local.server_env(dict(os.environ)) == {}
        assert tools.find("ngspice") == str(home / "undine-tools" / "bin" / "ngspice")


class FakeProc:
    def __init__(self, code=None):
        self.code, self.stopped = code, False

    def poll(self):
        return 0 if self.stopped else self.code

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


def test_ssh_forwards_a_port_and_reads_the_token_on_that_host():
    root, srv, la, _ = setup()
    started, ran, opened = [], [], []

    def popen(args, **kw):
        started.append(args)
        return FakeProc()

    def ssh_run(args, **kw):
        ran.append(args)
        return type("R", (), {"returncode": 0, "stdout": (srv.store.root / "mcp-token").read_text() + "\n", "stderr": ""})()

    tun = Tunnel("me@lab", "http://127.0.0.1:8700", popen=popen, run=ssh_run, probe=lambda port: True)
    la = Launcher("http://127.0.0.1:8700", start=True, opener=opened.append, http=la.http, ssh=tun)
    assert not la.start                                                    # a server elsewhere is never started from here
    res = run(la, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                   call(2, "open_changeset", title="t", reason="r")])
    a = started[0]
    assert a[0] == "ssh" and a[-1] == "me@lab" and "BatchMode=yes" in a and "-N" in a
    fwd = a[a.index("-L") + 1]
    assert fwd.startswith("127.0.0.1:") and fwd.endswith(":127.0.0.1:8700")
    local = f"http://127.0.0.1:{fwd.split(':')[1]}"
    assert ran == [["ssh", *Tunnel.OPTS, "me@lab", "cat", "--", f"{srv.store.root}/mcp-token"]]
    cid = json.loads(res[1]["result"]["content"][0]["text"])["id"]
    assert opened == [f"{local}/#/p/p?changes={cid}"]                      # the review opens through the tunnel
    assert tun.proc is None                                                # stdin closed: the tunnel is closed too
    shutil.rmtree(root)


def test_ssh_that_fails_says_why_and_how_to_fix_it():
    def popen(args, stderr, **kw):
        stderr.write(b"me@lab: Permission denied (publickey).\n")
        stderr.flush()
        return FakeProc(code=255)
    la = Launcher("http://127.0.0.1:8700", opener=lambda u: None, ssh=Tunnel("me@lab", "http://127.0.0.1:8700", popen=popen))
    res = run(la, [call(1, "list_changesets")])
    text = res[0]["result"]["content"][0]["text"]
    assert res[0]["result"]["isError"] and "Permission denied (publickey)" in text and "ssh me@lab true" in text


def test_ssh_to_a_host_where_undine_is_not_running_names_the_host():
    def down(*a, **k):
        raise OSError("connection refused")
    tun = Tunnel("lab", "http://127.0.0.1:8700", popen=lambda *a, **k: FakeProc(), probe=lambda port: True)
    la = Launcher("http://127.0.0.1:8700", opener=lambda u: None, http=down, ssh=tun)
    res = run(la, [call(1, "list_changesets")])
    text = res[0]["result"]["content"][0]["text"]
    assert "not running at http://127.0.0.1:8700 on lab" in text and "Start it there" in text


FAKE_SSH = """#!{python}
# ssh for the tests: the "remote host" is this machine. -L forwards for real; a command runs in a shell, as sshd does.
import socket, subprocess, sys, threading
args, fwd, i = sys.argv[1:], None, 0
while args[i].startswith("-"):
    if args[i] == "-L":
        fwd = args[i + 1]
    i += 2 if args[i] in ("-o", "-L") else 1
host, cmd = args[i], args[i + 1:]
if host == "bad":
    sys.exit(print("bad: Permission denied (publickey).", file=sys.stderr) or 255)
if cmd:
    sys.exit(subprocess.call(" ".join(cmd), shell=True))
bind, lport, rhost, rport = fwd.split(":")
srv = socket.create_server((bind, int(lport)))
def pipe(a, b):
    try:
        while (d := a.recv(65536)):
            b.sendall(d)
    except OSError:
        pass
    try:
        b.shutdown(socket.SHUT_WR)
    except OSError:
        pass
while True:
    c, _ = srv.accept()
    u = socket.create_connection((rhost, int(rport)))
    threading.Thread(target=pipe, args=(c, u), daemon=True).start()
    threading.Thread(target=pipe, args=(u, c), daemon=True).start()
"""


def test_a_real_ssh_process_forwards_and_reads_a_token_under_a_path_with_spaces(monkeypatch):
    d = Path(tempfile.mkdtemp())
    (d / "bin").mkdir()
    (d / "bin" / "ssh").write_text(FAKE_SSH.replace("{python}", sys.executable))
    (d / "bin" / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{d / 'bin'}{os.pathsep}{os.environ['PATH']}")
    root = d / "my projects"
    root.mkdir()
    (root / "mcp-token").write_text("tok-123\n")

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"undine": True, "root": str(root), "mcp": True}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    hs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=hs.serve_forever, daemon=True).start()
    remote = f"http://127.0.0.1:{hs.server_address[1]}"
    try:
        la = Launcher(remote, opener=lambda u: None, ssh=Tunnel("lab", remote))
        assert la.ensure_server(), la.problem
        assert la.token == "tok-123" and la.url != remote and la.ui_url == la.url and la.ssh.alive()
        proc = la.ssh.proc
        la.shutdown()
        assert proc.poll() is not None and la.ssh.proc is None
        bad = Launcher(remote, opener=lambda u: None, ssh=Tunnel("bad", remote))
        assert not bad.ensure_server() and "Permission denied" in bad.problem
    finally:
        hs.shutdown()
        shutil.rmtree(d)


def test_an_open_screen_follows_the_work_instead_of_a_new_tab():
    root, srv, la, opened = setup()
    c = TestClient(create_app(srv, None))
    assert c.get("/api/p/p/presence").json() == {"screen": False}
    assert c.get("/api/p/p/live").status_code == 200                              # a visible screen asks who is writing
    assert c.get("/api/p/p/presence").json() == {"screen": True}
    out = run(la, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                   call(2, "open_changeset", title="t", reason="r")])
    texts = [x["text"] for x in out[-1]["result"]["content"]]
    assert opened == [] and any("Undine is open on the user's screen" in t for t in texts)
    srv.screens.clear()                                                          # no screen: the launcher opens one
    run(la, [call(3, "open_changeset", title="u", reason="r")])
    assert len(opened) == 1
    shutil.rmtree(root)


def test_a_forgotten_session_starts_over_with_the_clients_name():
    root, srv, la, opened = setup()
    auth = {"authorization": f"Bearer {mcp.ensure_token(srv.store.root)}"}
    code, out, _ = mcp.handle(srv, mcp.Sessions(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                              {**auth, "mcp-session-id": "gone"})
    assert code == 404                                                           # MCP: the client must initialize again
    run(la, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "claude-desktop"}}}])
    la.session = "forgotten-by-a-restart"
    out = run(la, [call(2, "open_changeset", title="after restart", reason="r")])
    assert "result" in out[-1] and not out[-1]["result"]["isError"], out
    cs = next(c for c in tc.Changesets(srv).list("p") if c["title"] == "after restart")
    assert cs["actor"] == "agent:claude-desktop" and la.session != "forgotten-by-a-restart"
    shutil.rmtree(root)
