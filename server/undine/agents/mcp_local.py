"""Local MCP entry point (stdio) for desktop LLM clients: `python -m undine mcp-stdio`.

The client starts this process; it
* finds the Undine server (default http://127.0.0.1:8700) or starts one in the background with MCP on, and leaves it
  running when the chat ends, so the proposals and their evidence stay in Undine;
* relays the client's JSON-RPC messages to the server's /mcp endpoint with the bearer token; the server does all the work
  (working copies, the log, the one-writer lock, checks), so this process holds no design state;
* opens Undine at the changeset's review once, when the work on it starts (open_changeset), and offers `open_in_undine`
  to open it again on request. Nothing is pushed to the front later.
With `--ssh HOST` the server runs on another machine: this process forwards a local port to it over SSH, reads the token
there, and never starts a server (see Tunnel).
See docs/design/live-changesets.md and docs/guide/assistant.md.
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path, PurePosixPath

OPEN_TOOL = {
    "name": "open_in_undine",
    "description": "Open Undine on the user's screen: the review of a changeset (with changeset), or the project.",
    "inputSchema": {"type": "object", "properties": {
        "project": {"type": "string", "description": "the Undine project (real name)"},
        "changeset": {"type": "string", "description": "a changeset id, e.g. cs-1a2b3c"},
        "agent": {"type": "string", "description": "optional: your own name, as every tool takes it (several agents on "
                  "one connection); opening a screen does not depend on it"}}, "required": ["project"],
        "additionalProperties": False},
}


def _wait_s(default: float = 120.0) -> float:
    """$UNDINE_RESTART_WAIT_S in seconds; one that is not a number is said and the default taken (a typo must not stop
    every client)."""
    v = os.environ.get("UNDINE_RESTART_WAIT_S")
    try:
        return max(0.0, float(v)) if v else default
    except ValueError:
        print(f"undine: UNDINE_RESTART_WAIT_S is seconds, not {v!r}: waiting {default:g} s", file=sys.stderr)
        return default


# a server that refuses connections (or a proxy in front of it that says 502 / 503) is restarting, to deploy a change:
# a message is sent again every RESTART_POLL_S until it is back, for at most RESTART_WAIT_S (Launcher.send)
RESTART_WAIT_S = _wait_s()
RESTART_POLL_S = 1.0


class Away(ConnectionError):
    """The server did not come back within RESTART_WAIT_S (Launcher.send)."""


def refused(e: BaseException) -> bool:
    """Whether a message was refused because no server listens (it is restarting): it never reached one."""
    return isinstance(getattr(e, "reason", e), ConnectionRefusedError)


def cut(e: BaseException) -> bool:
    """Whether a connection was closed without an answer (reset, or closed before the answer: what the local end of an
    SSH forward does when the far side refuses), so that the server may be away."""
    return isinstance(getattr(e, "reason", e), (ConnectionResetError, ConnectionAbortedError, BrokenPipeError))


def log(msg: str) -> None:
    print(f"undine-mcp: {msg}", file=sys.stderr, flush=True)


def _env_file() -> dict[str, str]:
    """~/.undine/env (KEY=value lines), as scripts/restart.sh loads it, for a server this process starts."""
    p = Path(os.environ.get("UNDINE_ENV", Path.home() / ".undine" / "env"))
    out: dict[str, str] = {}
    if p.is_file():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


def server_env(env: dict[str, str]) -> dict[str, str]:
    """What a server this process starts needs of its environment besides `env`, as `pixi run` gives it (pixi.toml's
    activation): this interpreter's environment as the tool prefix (UNDINE_TOOLS), where its ngspice reads its spinit
    (SPICE_LIB_DIR). A client starts this process with the environment's Python but without pixi run, and the tools
    must not come from elsewhere (an older ~/undine-tools). Only an environment that holds the tools (tools.tool_env)
    is made the prefix: a virtual environment's or a system interpreter's server keeps looking in ~/undine-tools, as
    any server started without UNDINE_TOOLS does. A tool prefix `env` names (the user's ~/.undine/env) is kept, and
    with it whatever spinit it sets."""
    from undine import tools
    own = tools.tool_env()
    if env.get("UNDINE_TOOLS") or own is None:
        return {}
    out = {"UNDINE_TOOLS": str(own)}
    share = own / "share" / "ngspice"
    if not env.get("SPICE_LIB_DIR") and share.is_dir():
        out["SPICE_LIB_DIR"] = str(share)
    return out


def _wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(errors="ignore").lower()
    except OSError:
        return False


def open_url(url: str) -> None:
    """The user's browser: $UNDINE_OPEN (a command, {url} replaced), WSL's Windows browser, or the default one."""
    cmd = os.environ.get("UNDINE_OPEN")
    try:
        if cmd:
            subprocess.Popen([a.replace("{url}", url) for a in shlex.split(cmd)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif _wsl():
            subprocess.Popen(["cmd.exe", "/c", "start", "", url.replace("&", "^&")], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, cwd="/mnt/c" if Path("/mnt/c").is_dir() else None)
        else:
            webbrowser.open(url)
    except Exception as e:
        log(f"could not open a browser ({e}); open {url}")
        return
    log(f"opened {url}")


def _free_port(preferred: int) -> int:
    """`preferred` if nothing on this machine listens there (the review URLs then look like the server's), else any."""
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise OSError("no free local port")


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _tail(text: str, n: int = 3) -> str:
    return " / ".join(line.strip() for line in text.strip().splitlines()[-n:] if line.strip())


class Tunnel:
    """Undine on another machine (`--ssh HOST`: user@host or an ~/.ssh/config alias, so jump hosts and keys come from
    there): a local port forwarded to the server over SSH, and the token read on that machine. ssh runs without a
    terminal (stdin is the MCP channel), so it needs a key or an agent and a known host key, never a prompt."""
    OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

    def __init__(self, host: str, remote_url: str, popen=subprocess.Popen, run=subprocess.run, probe=_listening):
        self.host = host
        u = urllib.parse.urlparse(remote_url)
        self.scheme, self.rhost, self.rport = u.scheme or "http", u.hostname or "127.0.0.1", u.port or 8700
        self.popen, self.run, self.probe = popen, run, probe
        self.proc = None

    def hint(self) -> str:
        return (f"mcp-stdio runs ssh without a terminal, so it needs key authentication (a key without a passphrase, or an "
                f"ssh-agent) and a known host key: run `ssh {self.host} true` once in a terminal first")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def open(self, wait: float = 20.0) -> str:
        """Start the forward; the local URL once it listens. OSError (with ssh's own words) when it cannot."""
        self.close()
        port = _free_port(self.rport)
        args = ["ssh", *self.OPTS, "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-N", "-T",
                "-L", f"127.0.0.1:{port}:{self.rhost}:{self.rport}", self.host]
        log(f"forwarding 127.0.0.1:{port} to {self.rhost}:{self.rport} on {self.host}")
        with tempfile.TemporaryFile() as err:     # a file, not a pipe: a long-lived ssh must never block on stderr
            self.proc = self.popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if (code := self.proc.poll()) is not None:
                    self.proc = None
                    err.seek(0)
                    said = _tail(err.read().decode(errors="replace")) or f"exit code {code}"
                    raise OSError(f"ssh {self.host} failed: {said}. {self.hint()}")
                if self.probe(port):
                    return f"{self.scheme}://127.0.0.1:{port}"
                time.sleep(0.1)
        self.close()
        raise OSError(f"ssh {self.host}: no connection after {wait:.0f} s. {self.hint()}")

    def read(self, path: str) -> str:
        r = self.run(["ssh", *self.OPTS, self.host, "cat", "--", shlex.quote(path)], stdin=subprocess.DEVNULL,
                     capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise OSError(f"could not read {path} on {self.host}: {_tail(r.stderr) or f'exit code {r.returncode}'}")
        return r.stdout

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


class Launcher:
    def __init__(self, url: str, ui_url: str | None = None, start: bool = True, open_review: bool = True,
                 root: Path | None = None, opener=open_url, http=None, ssh: Tunnel | None = None, token: str | None = None):
        self.url = url.rstrip("/")            # with ssh: the server as seen on that host, then the tunnel's local end
        self.remote = self.url
        self.ui_given = ui_url
        self.ui_url = (ui_url or self.url).rstrip("/")
        self.start, self.open_review, self.root = start and ssh is None, open_review, root
        self.ssh, self.fixed_token = ssh, token
        self.opener = opener
        self.http = http or self._http        # (method, path, body, headers) -> (status, json | None, headers)
        self.session: str | None = None
        self.token: str | None = None
        self.opened: set[str] = set()         # changesets whose review was opened: once each
        self.problem: str | None = None       # why the server cannot be used (told to the client per call)
        self.timeout = 600.0                  # seconds for one answer (a call that waits longer raises it, _relay)
        self.code: int | None = None          # the HTTP status of the last answer (502 / 503: a proxy, the server away)
        # "<project>|<agent>" -> the changeset open_changeset opened (or continued) in this session: a new session (the
        # server forgot this one) continues them, so the agent's next edit still goes where it went (_reopen)
        self.changesets: dict[str, str] = {}

    # ------------------------------------------------------------------ the server

    def _http(self, method: str, path: str, body=None, headers=None):
        req = urllib.request.Request(self.url + path, method=method, headers={"content-type": "application/json", **(headers or {})},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None), dict(r.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw), dict(e.headers)
            except ValueError:
                return e.code, {"detail": raw.decode(errors="replace")}, dict(e.headers)

    def health(self) -> dict | None:
        try:
            code, out, _ = self.http("GET", "/api/health")
            return out if code == 200 and isinstance(out, dict) and out.get("undine") else None
        except OSError:
            return None

    def ensure_server(self, restarting: bool = False) -> bool:
        """Reuse a running Undine, or start one (detached: it outlives this process). False with self.problem set.
        `restarting`: a session was open with it, so a server that does not answer is taken to be restarting and waited
        for (RESTART_WAIT_S) before one is started or it is said to be missing. With ssh: (re)open the tunnel first; a
        server on another machine is never started from here."""
        if self.ssh is not None and not self.ssh.alive():
            try:
                self.url = self.ssh.open()
            except OSError as e:
                self.problem = str(e)
                return False
            self.ui_url = (self.ui_given or self.url).rstrip("/")
        h = self.health()
        back = time.monotonic() + (RESTART_WAIT_S if restarting else 0.0)
        while h is None and time.monotonic() < back:
            time.sleep(RESTART_POLL_S)
            h = self.health()
        if h is None and self.start:
            port = urllib.parse.urlparse(self.url).port or 8700
            env = {**os.environ, **_env_file(), "UNDINE_MCP": "1"}
            env.update(server_env(env))
            pkg = Path(__file__).resolve().parents[2]          # server/: where the undine package is
            env["PYTHONPATH"] = os.pathsep.join([str(pkg), env.get("PYTHONPATH", "")]).strip(os.pathsep)
            args = [sys.executable, "-m", "undine"] + (["--root", str(self.root)] if self.root else []) + ["serve", "--port", str(port), "--mcp"]
            logf = Path(os.environ.get("UNDINE_LOG", Path.home() / ".undine" / "serve.log"))
            logf.parent.mkdir(parents=True, exist_ok=True)
            log(f"starting Undine at {self.url} (log: {logf})")
            with logf.open("ab") as fh:
                subprocess.Popen(args, cwd=pkg, env=env, stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, start_new_session=True)
            for _ in range(120):
                time.sleep(0.25)
                if (h := self.health()):
                    break
        where = f"{self.remote} on {self.ssh.host}" if self.ssh else self.url
        if h is None:
            self.problem = (f"Undine is not running at {where}" + ("" if self.start or self.ssh else " (starting it is turned off)") +
                            ". Start it" + (" there" if self.ssh else "") + " with UNDINE_MCP=1 ./scripts/restart.sh")
            return False
        if not h.get("mcp"):
            self.problem = (f"Undine at {where} runs without MCP. Restart it with MCP on: UNDINE_MCP=1 ./scripts/restart.sh "
                            "(this launcher does not restart a server it did not start)")
            return False
        try:
            self.token = self._read_token(h["root"])
        except (OSError, subprocess.TimeoutExpired) as e:
            self.problem = f"Undine at {where} runs, but its MCP token could not be read: {e}"
            return False
        self.problem = None
        return True

    def _read_token(self, root: str) -> str:
        """$UNDINE_MCP_TOKEN, or <projects root>/mcp-token where the server runs (over ssh when that is elsewhere)."""
        if self.fixed_token:
            return self.fixed_token
        if self.ssh is not None:
            return self.ssh.read(str(PurePosixPath(root) / "mcp-token")).strip()
        return (Path(root) / "mcp-token").read_text().strip()

    def review_url(self, project: str, changeset: str | None = None) -> str:
        q = f"?changes={urllib.parse.quote(changeset)}" if changeset else ""
        return f"{self.ui_url}/#/p/{urllib.parse.quote(project)}{q}"

    # ------------------------------------------------------------------ relaying

    def send(self, msg: dict) -> dict | None:
        """_relay, and again while the server restarts: a message refused because no server listens (connection
        refused), or that a proxy in front answers 503 for, never reached one, so it is sent again every RESTART_POLL_S
        until the server is back, for at most RESTART_WAIT_S (then Away); one a proxy answers 502 for may have, so it
        is sent again only when doing it twice cannot do harm (a request id makes a write act once, and a message that
        is not a tool call changes nothing). Through an SSH forward a refused connection is accepted and then closed
        (the forward's local end): a message so cut that can be sent twice is sent again while the server does not
        answer its health check through the forward."""
        back = None
        p = msg.get("params") or {}
        again = msg.get("method") != "tools/call" or bool((p.get("arguments") or {}).get("request_id"))
        while True:
            try:
                out = self._relay(msg)
                if not (self.code == 503 or (self.code == 502 and again)):
                    return out
                why = f"Undine answered {self.code}"
            except OSError as e:
                if not (refused(e) or (self.ssh is not None and again and cut(e) and self.health() is None)):
                    raise
                why = str(e)
            back = back or time.monotonic() + RESTART_WAIT_S
            if time.monotonic() >= back:
                raise Away(f"it was not back after {RESTART_WAIT_S:g} s ({why})")
            time.sleep(RESTART_POLL_S)

    def _relay(self, msg: dict) -> dict | None:
        if msg.get("method") == "initialize":
            self.init_msg = msg                       # to start over if the server forgets the session (a restart)
        headers = {"authorization": f"Bearer {self.token}"}
        if self.session:
            headers["mcp-session-id"] = self.session
        # a tool call that waits (wait_for timeout_s, run_simulation wait_s) gets its answer that much later
        args = (msg.get("params") or {}).get("arguments") or {} if msg.get("method") == "tools/call" else {}
        waits = [0.0]
        for k in ("timeout_s", "wait_s"):
            try:
                waits.append(float(args[k]))
            except (KeyError, TypeError, ValueError):
                pass
        self.timeout = max(600.0, max(waits) + 120.0)
        self.code = None
        code, out, hdr = self.http("POST", "/mcp", msg, headers)
        self.code = code
        if code == 404 and self.session and getattr(self, "init_msg", None) and msg.get("method") != "initialize":
            # the server does not know this session any more (it forgot it, or it is another server): MCP says start
            # a new one, so do it for the client, who keeps its name, continue its changesets, and send the message again
            log("the server forgot the MCP session; starting a new one")
            self.session = None
            self._relay({**self.init_msg, "id": f"reinit-{msg.get('id')}"})
            self._relay({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._reopen(msg.get("id"))
            return self._relay(msg)
        sid = {k.lower(): v for k, v in hdr.items()}.get("mcp-session-id")
        if sid:
            self.session = sid
        if code == 202 or msg.get("id") is None:
            return None
        if code != 200 or out is None:
            return {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32000, "message": f"Undine answered {code}: {out}"}}
        if msg.get("method") == "tools/call" and (msg.get("params") or {}).get("name") == "open_changeset":
            self._opened(msg["params"].get("arguments") or {}, out)
        return out

    @staticmethod
    def _cs_key(args: dict) -> str:
        return f"{args.get('project') or ''}|{args.get('agent') or ''}"

    def _opened(self, args: dict, out: dict) -> None:
        """open_changeset answered: the changeset it opened or continued is this agent's in the project now."""
        res = out.get("result") or {}
        try:
            cid = None if res.get("isError") else json.loads(res["content"][0]["text"]).get("id")
        except (KeyError, IndexError, ValueError, AttributeError, TypeError):
            return
        if isinstance(cid, str) and cid and args.get("project"):
            self.changesets[self._cs_key(args)] = cid

    def _reopen(self, why) -> None:
        """A new session in place of one the server forgot: each changeset the old one had open is continued
        (open_changeset id=...), per project and agent; one that cannot be (closed meanwhile) is forgotten."""
        for key, cid in list(self.changesets.items()):
            project, _, agent = key.partition("|")
            args = {"project": project, "id": cid, **({"agent": agent} if agent else {})}
            try:
                out = self._relay({"jsonrpc": "2.0", "id": f"reopen-{why}-{cid}", "method": "tools/call",
                                   "params": {"name": "open_changeset", "arguments": args}})
            except OSError:
                return
            if not out or "error" in out or (out.get("result") or {}).get("isError"):
                self.changesets.pop(key, None)
                log(f"could not continue the changeset {cid} of {project} in the new session")
            else:
                log(f"continued the changeset {cid} of {project} in the new session")

    def close(self) -> None:
        """Ends the MCP session (the chat closed or started over): the server frees the changesets it was writing."""
        if self.session and self.token:
            try:
                self.http("DELETE", "/mcp", None, {"authorization": f"Bearer {self.token}", "mcp-session-id": self.session})
            except OSError:
                pass
        self.session = None
        self.changesets = {}

    def shutdown(self) -> None:
        """This process ends: the session, and the tunnel if there is one."""
        self.close()
        if self.ssh is not None:
            self.ssh.close()

    def handle(self, msg: dict) -> dict | None:
        m, i = msg.get("method"), msg.get("id")
        if m in ("resources/list", "resources/read") and i is not None:
            return self._guide(m, i, msg.get("params") or {})
        if m == "initialize" and self.session:
            self.close()                              # a new session in the same process: the old one is over
        if self.token is None and not self.ensure_server(restarting=self.session is not None):
            if m == "initialize":                     # still say hello, so the client can show the problem per call
                return {"jsonrpc": "2.0", "id": i, "result": {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or "2025-03-26",
                        "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}}, "serverInfo": {"name": "undine", "version": "0.1"},
                        "instructions": self.problem}}
            if i is None:
                return None
            if m == "tools/list":
                return {"jsonrpc": "2.0", "id": i, "result": {"tools": [OPEN_TOOL]}}
            return {"jsonrpc": "2.0", "id": i, "result": {"isError": True, "content": [{"type": "text", "text": self.problem}]}}
        if m == "tools/call" and (msg.get("params") or {}).get("name") == OPEN_TOOL["name"]:
            a = (msg.get("params") or {}).get("arguments") or {}
            url = self.review_url(a.get("project", ""), a.get("changeset"))
            self.opener(url)
            return {"jsonrpc": "2.0", "id": i, "result": {"content": [{"type": "text", "text": f"opened {url}"}], "isError": False}}
        out = self.send(msg)                          # waits while the server restarts
        if out and m == "tools/list" and "result" in out:
            out["result"]["tools"] = [*out["result"].get("tools", []), OPEN_TOOL]
        if out and m == "tools/call" and (msg.get("params") or {}).get("name") == "open_changeset":
            self._on_changeset(msg["params"].get("arguments") or {}, out)
        return out

    def _guide(self, m: str, i, params: dict) -> dict:
        """The agent guide (skills.py), from this checkout: readable even when the server is not, since it says how to connect."""
        from undine.agents import skills

        if m == "resources/list":
            return {"jsonrpc": "2.0", "id": i, "result": {"resources": skills.resources()}}
        uri = params.get("uri", "")
        try:
            return {"jsonrpc": "2.0", "id": i, "result": {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": skills.read(uri)}]}}
        except KeyError as e:
            return {"jsonrpc": "2.0", "id": i, "error": {"code": -32002, "message": str(e).strip("'\"")}}

    def _on_changeset(self, args: dict, out: dict) -> None:
        """Work on a changeset starts: show its review, once, and tell the client where it is."""
        res = out.get("result") or {}
        if res.get("isError"):
            return
        try:
            cid = json.loads(res["content"][0]["text"]).get("id")
        except (KeyError, IndexError, ValueError, AttributeError):
            return
        if not cid or not args.get("project"):
            return
        url = self.review_url(args["project"], cid)
        if self._screen_open(args["project"]):        # an open screen follows the work by itself: no second tab
            res["content"].append({"type": "text", "text": f"Undine is open on the user's screen and follows this changeset as "
                                                            f"you work; its review: {url}"})
            self.opened.add(cid)
            return
        res["content"].append({"type": "text", "text": f"The user can watch and review this changeset in Undine: {url}"})
        if self.open_review and cid not in self.opened:
            self.opened.add(cid)
            self.opener(url)

    def _screen_open(self, project: str) -> bool:
        try:
            code, out, _ = self.http("GET", f"/api/p/{urllib.parse.quote(project)}/presence")
            return code == 200 and bool((out or {}).get("screen"))
        except OSError:
            return False

    def serve_stdio(self, stdin=sys.stdin, stdout=sys.stdout) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
                stdout.flush()
                continue
            try:
                out = self.handle(msg)
            except Exception as e:  # e.g. the server went away: say so, keep the session
                self.token = None
                out = None if msg.get("id") is None else {"jsonrpc": "2.0", "id": msg.get("id"),
                                                          "error": {"code": -32000, "message": f"Undine is not reachable: {e}"}}
            if out is not None:
                stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
                stdout.flush()
        self.shutdown()                               # stdin closed: the client is gone


def main(args) -> None:
    url = args.url or os.environ.get("UNDINE_URL") or "http://127.0.0.1:8700"
    host = args.ssh or os.environ.get("UNDINE_SSH") or None
    la = Launcher(url, args.ui_url or os.environ.get("UNDINE_UI_URL"), start=not args.no_start, open_review=not args.no_open,
                  root=args.root, ssh=Tunnel(host, url) if host else None, token=os.environ.get("UNDINE_MCP_TOKEN") or None)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # the client stops this process: close the tunnel as well
    try:
        la.serve_stdio()
    finally:
        la.shutdown()
