"""Undine from scripts and the shell: the tools the in-app assistant and MCP clients use (agent_tools), over the server's
/mcp endpoint. Standard library only, so it runs in any Python 3.10+ that can import this package.

    from undine.agents.client import Undine         # or: from undine.client import Undine
    with Undine(project="demo", agent="layout-1") as u:    # agent: who you are (the shell's --session NAME)
        u.get_netlist(lib="mylib", cell="inv")
        u.open_changeset(title="Wider pull-down", reason="the fall time misses the spec")
        u.set_instance_params(lib="mylib", cell="inv", instance="M2", params={"W": "1.5"}, reason="faster fall")
        u.run_checks()

    $ undine call get_netlist --project demo lib=mylib cell=inv
    $ undine call set_instance_params --project demo lib=mylib cell=inv instance=M2 params:='{"W": "1.5"}' reason="faster fall"
    $ undine call batch --project demo ops:=@ops.json reason="place the CDAC"

Results come whole to a script; `undine call` prints at most --max-chars of one, shortened inside and still valid JSON
(results.py), since an agent reads it from its shell.

The client holds no design state. The server does the work (changesets, the log, the one-writer lease, checks) and
records every result that leaves it in the project's llm/audit.jsonl, as for any MCP client. Edits go only into a
changeset's working copy; a person reviews and applies it in Undine. Flexibility comes from composing these typed tools
in a script, not from evaluating code in the server.

The shell keeps one MCP session per name (--session, default "default") between calls, in ~/.undine/cli-sessions.json
($UNDINE_CLI_SESSIONS), so a changeset opened by one `undine call` is still yours in the next one; `undine session end`
lets it go. A named session is an agent of that name (its calls carry agent=NAME unless they give their own): the
history, the review and the write lock name it ("agent:undine-cli/NAME"), and two named sessions are two writers even
when they share a sessions file. A session named before this (whose changeset the session's unnamed agent holds) hands
that changeset to its name at its first call (mcp.Sessions.ctx: own). The server keeps a session across a restart; if
it does not know the session any more, the client starts a new one and continues there the changesets it had open (by
project and agent). A call the server refuses while it restarts (connection refused, 502 / 503) is sent again, with
the same request id, until it is back (mcp_local.RESTART_WAIT_S, $UNDINE_RESTART_WAIT_S).

An Undine on another machine is reached over SSH (ssh="user@host", --ssh, or $UNDINE_SSH), as `undine mcp-stdio --ssh`
does: a local port forwarded while the client runs, the token read on that host. Review URLs are as seen on that host.
"""
from __future__ import annotations

import atexit
import base64
import contextlib
import json
import os
import sys
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from undine.agents import results
from undine.agents.mcp_local import Away, Launcher, Tunnel

PROTOCOL = "2025-03-26"
DEFAULT_URL = "http://127.0.0.1:8700"
RETRIES = (1.0, 3.0)        # waits before the 2nd and 3rd attempt of a call whose answer was lost
# What `undine call` prints of one result, in characters: an agent reads it from its shell, whose output is cut at
# about 30000 (Claude Code); a longer result is shortened inside and stays valid JSON (results.py). The server sends
# the whole result to both clients (a script gets all of it); --max-chars 0 prints all of it (into a file, or jq).
SHELL_CHARS = 25000
AGENT_NAME = re.compile(r"[\w.-]{1,64}")      # an agent's name (mcp.AGENT_NAME): a --session name that is one is the agent
SESSION_ID = re.compile(r"[0-9a-f]{32}")      # an MCP session's id, as the server makes it (mcp.py)


class UndineError(RuntimeError):
    """Undine cannot be reached or used (not running, MCP off, a protocol error)."""


class ToolError(UndineError):
    """A tool refused or failed; the message says why (e.g. "no changeset is open. Call open_changeset first ...")."""


class Undine:
    """A connection to a running Undine. Tools are methods: `u.get_netlist(lib=..., cell=...)` is
    `u.call("get_netlist", lib=..., cell=...)`. Results come back parsed (a dict or list) when the tool answers JSON."""

    def __init__(self, project: str | None = None, url: str | None = None, *, start: bool = False, session: str | None = None,
                 client: str = "undine-python", root: Path | None = None, http=None, ssh: str | Tunnel | None = None,
                 max_chars: int | None = None, agent: str | None = None):
        self.project = project or os.environ.get("UNDINE_PROJECT")
        if session is not None and not SESSION_ID.fullmatch(session):
            raise UndineError(f"session is the id of an MCP session (u.session of another client: 32 hex digits), not "
                              f"{session!r}; the shell's --session NAME is the agent NAME here: Undine(agent={session!r})"
                              if AGENT_NAME.fullmatch(session) else
                              f"session is the id of an MCP session (u.session of another client: 32 hex digits), not {session!r}")
        self.client = client
        # who this client is when several share a session (the MCP tools' `agent`): every call carries it unless it
        # gives its own; the history, the review and the write lock name it
        if agent is not None and not AGENT_NAME.fullmatch(agent):
            raise UndineError(f"agent is a name of up to 64 letters, digits, '.', '_' or '-' (e.g. layout-1), not {agent!r}")
        self.agent = agent
        self.max_chars = max_chars             # None: whole results (an MCP client gets mcp.RESULT_CHARS)
        self.last_request_id: str | None = None
        url = url or os.environ.get("UNDINE_URL") or DEFAULT_URL
        ssh = ssh or os.environ.get("UNDINE_SSH") or None
        if isinstance(ssh, str):
            ssh = Tunnel(ssh, url)
        if ssh is not None:
            atexit.register(ssh.close)                 # a script that never closes must not leave ssh running
        # with ssh, review URLs are as seen on that host: the forward ends with this client, the review comes later
        self._l = Launcher(url, ui_url=url if ssh else None, start=start, open_review=False, root=root,
                           opener=lambda _url: None, http=http, ssh=ssh, token=os.environ.get("UNDINE_MCP_TOKEN") or None)
        self._l.session = session
        # how to start a session: also used to start over when the server forgot a kept one (it restarted)
        self._l.init_msg = {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": client, "version": "0.1"}}}
        self._id = 0
        self.last_replayed = False

    @property
    def url(self) -> str:
        return self._l.url

    @property
    def where(self) -> str:
        """The server as the user named it: its URL, and the host it runs on when reached over SSH."""
        return f"{self._l.remote} on {self._l.ssh.host}" if self._l.ssh else self._l.remote

    @property
    def session(self) -> str | None:
        """The MCP session id: pass it as `session=` to continue this session (and its changeset) elsewhere."""
        return self._l.session

    def review_url(self, changeset: str | None = None, project: str | None = None) -> str:
        """Where a person reviews a changeset (or opens the project) in Undine."""
        return self._l.review_url(project or self.project or "", changeset)

    def _rpc(self, method: str, params: dict) -> dict:
        closed = self._l.ssh is not None and not self._l.ssh.alive()     # disconnected, or the forward dropped
        # a kept session: the server was there, so one that does not answer now is restarting (waited for)
        if (self._l.token is None or closed) and not self._l.ensure_server(restarting=self._l.session is not None):
            raise UndineError(self._l.problem)
        if not self._l.session:
            out = self._send("initialize", self._l.init_msg["params"])
            if not self._l.session:
                raise UndineError(f"Undine did not start an MCP session: {out}")
            self._l._relay({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self._send(method, params)

    def _send(self, method: str, params: dict) -> dict:
        """Sends a message until it is answered: again after a lost answer (RETRIES), and for as long as the server
        restarts (Launcher.send: mcp_local.RESTART_WAIT_S). Every call can be sent again: a write carries its
        request_id, so the server does not act twice, and a server that forgot the session starts a new one that goes
        on with its changesets (Launcher._reopen)."""
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        for wait in (*RETRIES, None):
            try:
                out = self._l.send(msg)
                break
            except Away as e:
                raise UndineError(f"Undine at {self.where} did not answer {method}: {e}") from e
            except OSError as e:      # the answer was lost (a timeout, a dropped connection): every call can be sent again,
                if wait is None:      # a write carries its request_id, so the server does not act twice
                    raise UndineError(f"Undine at {self.where} did not answer {method}: {e}") from e
                time.sleep(wait)
                if self._l.ssh is not None and not self._l.ssh.alive():
                    self._l.ensure_server()           # the SSH forward dropped: open it again before sending again
        if not out or "error" in out:
            raise UndineError((out or {}).get("error", {}).get("message", f"no answer to {method}"))
        return out["result"]

    def tools(self) -> list[dict]:
        """Every tool with its description and JSON schema (`project` is added by the client)."""
        return self._rpc("tools/list", {})["tools"]

    def call(self, tool: str, /, **args: Any) -> Any:
        """Calls a tool. Every call carries a request_id (yours, or a new one; `last_request_id`), so a write is never
        applied twice when the answer is lost and the call is sent again; `last_replayed` says whether the answer was a
        first call's. Results come whole, unless the client was made with max_chars (then shortened as an MCP
        client's are: valid JSON, `_truncated` says what was left out)."""
        project = args.pop("project", None) or self.project
        if not project:
            raise UndineError("which project? Pass project=... (or set $UNDINE_PROJECT)")
        rid = self.last_request_id = args.pop("request_id", None) or uuid.uuid4().hex
        if self.agent and "agent" not in args:          # agent="" given: the session's unnamed agent
            args["agent"] = self.agent
        meta = {"undine/max_chars": self.max_chars or 0}
        if getattr(self, "_own", None) and args.get("agent") == self._own:
            # the first call of a session named before its name was its agent (_client): its changesets were kept as
            # the unnamed agent's ("<project>|"); the name takes them over, on the server (mcp.Sessions.ctx) and for a
            # new session that continues them (Launcher._reopen). Once only: an unnamed agent= changeset opened later
            # stays the unnamed agent's
            meta["undine/own_agent"] = True
            kept = self._l.changesets
            for k in [k for k in kept if k.endswith("|") and k + self._own not in kept]:
                kept[k + self._own] = kept.pop(k)
            self._own = None
        res = self._rpc("tools/call", {"name": tool, "arguments": {"project": project, "request_id": rid, **args},
                                       "_meta": meta})
        self.last_replayed = bool((res.get("_meta") or {}).get("undine/replayed"))
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise ToolError(text)
        images = [base64.b64decode(c["data"]) for c in res.get("content", []) if c.get("type") == "image" and c.get("data")]
        try:
            value = json.loads(text)
        except ValueError:
            value = text
        if images:                                     # a picture: its PNG bytes beside the numbers
            value = {**(value if isinstance(value, dict) else {"text": value}), "images": images}
        return value

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda **args: self.call(name, **args)

    def close(self) -> None:
        """End the session: the server lets go of the changeset it was writing (it stays open for review or a later session).
        Also closes the SSH forward, if there is one."""
        self._l.shutdown()

    def disconnect(self) -> None:
        """Close the SSH forward but keep the session, to continue it later (as `undine call` does between calls)."""
        if self._l.ssh is not None:
            self._l.ssh.close()

    def __enter__(self) -> "Undine":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------------------------- the shell (undine call)

def parse_args(items: list[str]) -> dict:
    """key=value is a string, key:=json any JSON value (number, list, object, true), key=@path the text of a file,
    key:=@path the JSON value in a file."""
    out: dict = {}
    for it in items:
        k, sep, v = it.partition(":=")
        if sep and "=" not in k:
            out[k] = (_json(_file(v[1:], f"{k}:={v}"), f"{k}:={v}") if v.startswith("@") else
                      _json(v, f"{k}:=", f"; for a string use {k}=..."))
            continue
        k, sep, v = it.partition("=")
        if not sep or not k:
            raise UndineError(f"expected key=value, key:=json, key=@file or key:=@file.json, not {it!r}")
        out[k] = _file(v[1:], k) if v.startswith("@") else v
    return out


def _file(path: str, what: str) -> str:
    try:
        return Path(path).expanduser().read_text()
    except OSError as e:
        raise UndineError(f"{what}: cannot read {path} ({e.strerror or e})") from None


def _json(text: str, what: str, hint: str = ""):
    try:
        return json.loads(text)
    except ValueError as e:
        raise UndineError(f"{what} needs a JSON value ({e}){hint}") from None


def _sessions_file() -> Path:
    return Path(os.environ.get("UNDINE_CLI_SESSIONS", Path.home() / ".undine" / "cli-sessions.json"))


def _load_sessions() -> dict:
    try:
        return json.loads(_sessions_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_sessions(s: dict) -> None:
    """Writes the kept sessions whole (through a file of its own, so that a reader never sees half of it)."""
    f = _sessions_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(f)


@contextlib.contextmanager
def _sessions_held():
    """The kept sessions, read and written back by one process at a time (`undine call`s run side by side: agents
    sharing a --session with agent=NAME each); what the block changes in the dict it gets is saved."""
    f = _sessions_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f.with_name(f.name + ".lock"), "a") as lk:
        try:
            import fcntl
            fcntl.flock(lk, fcntl.LOCK_EX)
        except (ImportError, OSError):            # no flock here: as before, the last writer wins
            pass
        s = _load_sessions()
        before = json.dumps(s, sort_keys=True)
        yield s
        if json.dumps(s, sort_keys=True) != before:
            _save_sessions(s)


def _kept(s: dict, key: str) -> dict:
    """A kept session: {"session": its id, "changesets": {"<project>|<agent>": the changeset open in it}} (an id alone
    in files of earlier versions)."""
    v = s.get(key)
    if isinstance(v, str):
        return {"session": v, "changesets": {}}
    if isinstance(v, dict) and isinstance(v.get("changesets"), dict):
        return {"session": v.get("session"),
                "changesets": {k: c for k, c in v["changesets"].items() if isinstance(c, str)},
                **({"named": True} if v.get("named") is True else {})}
    return {"session": None, "changesets": {}}


def session_agent(name: str | None) -> str | None:
    """The agent a --session name makes its calls (None: the default session, or a name that is no agent name)."""
    return name if name and name != "default" and AGENT_NAME.fullmatch(name) else None


def _client(args, http=None) -> tuple[Undine, str]:
    name = getattr(args, "session", None) or "default"
    u = Undine(getattr(args, "project", None), args.url, start=args.start, client="undine-cli", root=getattr(args, "root", None),
               http=http, ssh=getattr(args, "ssh", None), agent=session_agent(name))
    key = f"{u.where}|{name}"
    kept = _kept(_load_sessions(), key)
    u._l.session, u._l.changesets = kept["session"], dict(kept["changesets"])
    # a session named before its name was its agent (kept by an earlier client: not "named") goes on under its name:
    # its own agent's first call takes over what the unnamed agent has open (Undine.call: undine/own_agent); a session
    # this client made, or one already taken over, never again (its agent= calls are the unnamed agent's for good)
    u._own = u.agent if u.agent and kept["session"] and not kept.get("named") else None
    u._kept = kept                                  # as this call found it: _remember saves what it changed
    return u, key


def _remember(u: Undine, key: str) -> None:
    """Keeps the session for the next call, with the changesets open in it: a server that no longer knows the session
    gets a new one that goes on with them (mcp_local.Launcher._reopen). Only what this call changed is written over
    what the file holds now: another call on the same --session (another agent's, agent=NAME) may have opened a
    changeset meanwhile."""
    base = getattr(u, "_kept", None) or {"session": None, "changesets": {}}
    with _sessions_held() as s:
        cur = _kept(s, key)
        cs = dict(cur["changesets"])
        for k, cid in u._l.changesets.items():
            if base["changesets"].get(k) != cid:
                cs[k] = cid
        for k, cid in base["changesets"].items():
            if k not in u._l.changesets and cs.get(k) == cid:      # this call let it go (it could not be continued)
                cs.pop(k)
        now = {"session": u.session if u.session != base["session"] else cur["session"] or u.session,
               "changesets": cs}
        if cur.get("named") or (u.agent and not getattr(u, "_own", None)):     # taken over, or made by this client
            now["named"] = True
        if cur != now or s.get(key) != now:
            s[key] = now


SHELL_HOW = "--max-chars 0 prints all of it (into a file, or jq); the tool's filters or offset (undine tools NAME) ask for less"


def _show(value: Any, raw: bool, out, limit: int | None = None) -> None:
    """The result as JSON (indented, or on one line with raw; a text result as it is), shortened inside to `limit`
    characters and still valid (results.py)."""
    dumps = results.compact if raw else (lambda v: json.dumps(v, indent=2, ensure_ascii=False))
    out.write(results.text(value, limit, SHELL_HOW, dumps))
    out.write("\n")


def _hint(e: Exception, args) -> str:
    msg = str(e)
    remote = getattr(args, "ssh", None) or os.environ.get("UNDINE_SSH")
    return msg + " (or pass --start)" if "is not running" in msg and not args.start and not remote else msg


def main_call(args, http=None, out=sys.stdout, err=sys.stderr) -> int:
    rid = None
    try:
        tool_args = parse_args(args.args)
        if args.json_args:
            src = args.json_args
            text = sys.stdin.read() if src == "-" else _file(src[1:], "--json") if src.startswith("@") else src
            given = _json(text, "--json")
            if not isinstance(given, dict):
                raise UndineError("--json needs a JSON object of the arguments, {\"lib\": ..., ...}")
            tool_args = {**given, **tool_args}
        # the id is known before the call (--request-id, the arguments' request_id, else a new one) and said first, so
        # that a call cut off (a shell's timeout) or whose answer is lost can be sent again with it without acting twice
        rid = tool_args["request_id"] = getattr(args, "request_id", None) or tool_args.get("request_id") or uuid.uuid4().hex
        u, key = _client(args, http)
        who = tool_args["agent"] if "agent" in tool_args else u.agent
        err.write(f"undine: request id {rid}" + (f" (agent {who})" if who else "") + "\n")
        try:
            value = u.call(args.tool, **tool_args)
        finally:
            if u.session:
                _remember(u, key)
            u.disconnect()
    except ToolError as e:
        err.write(f"undine: {args.tool}: {e}\n")
        return 1
    except (UndineError, OSError, ValueError) as e:
        again = (f" (request id {rid}: whether it acted is unknown; the same call with --request-id {rid} acts at most "
                 "once)") if rid and "did not answer" in str(e) else ""
        err.write(f"undine: {_hint(e, args)}{again}\n")
        return 2
    if args.tool == "open_changeset" and isinstance(value, dict) and value.get("id"):
        value = {**value, "review": u.review_url(value["id"])}   # where a person watches and reviews it
    if isinstance(value, dict) and value.get("images"):     # pictures go to files; the output says where
        value = {**value, "images": _save_images(value["images"], args.out, args.tool, getattr(args, "out_dir", None))}
    _show(value, args.raw, out, getattr(args, "max_chars", None))
    return 0


def picture_dir(out_dir: str | None = None) -> Path:
    """Where `undine call` writes a picture it is not told a file for: --out-dir, $UNDINE_OUT_DIR, the folder of
    $UNDINE_CLI_SESSIONS (an agent with a sessions file of its own has a folder of its own), else the current
    directory; never the shared temporary directory."""
    d = out_dir or os.environ.get("UNDINE_OUT_DIR")
    if not d and os.environ.get("UNDINE_CLI_SESSIONS"):
        d = str(Path(os.environ["UNDINE_CLI_SESSIONS"]).expanduser().parent)
    return Path(d or ".").expanduser()


def _save_images(images: list[bytes], out: str | None, tool: str, out_dir: str | None = None) -> list[str]:
    """The pictures written to files: `out` (a file, its name numbered when there are several; a directory, or a path
    ending in '/', takes new files), else new files in picture_dir()."""
    paths = []
    for i, png in enumerate(images):
        if out and not (out.endswith(("/", os.sep)) or Path(out).expanduser().is_dir()):
            p = Path(out).expanduser()
            if len(images) > 1:
                p = p.with_name(f"{p.stem}-{i + 1}{p.suffix or '.png'}")
            p.parent.mkdir(parents=True, exist_ok=True)
        else:
            d = Path(out).expanduser() if out else picture_dir(out_dir)
            d.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f"undine-{tool}-", suffix=".png", dir=d)
            os.close(fd)
            p = Path(name)
        p.write_bytes(png)
        paths.append(str(p.resolve()))
    return paths


def main_tools(args, http=None, out=sys.stdout, err=sys.stderr) -> int:
    try:
        u, key = _client(args, http)
        try:
            tools = u.tools()
            _remember(u, key)
        finally:
            u.disconnect()
    except (UndineError, OSError) as e:
        err.write(f"undine: {_hint(e, args)}\n")
        return 2
    if args.name:
        t = next((t for t in tools if t["name"] == args.name), None)
        if t is None:
            err.write(f"undine: no tool {args.name!r}; `undine tools` lists them\n")
            return 1
        if args.json:
            _show(t, False, out)
            return 0
        s = t["inputSchema"]
        out.write(f"{t['name']}\n\n{t['description']}\n\narguments:\n")
        for k, p in s.get("properties", {}).items():
            typ = results.shape(p)                     # with the shape of list items and objects: [{kind, id}, ...]
            req = " (required)" if k in s.get("required", []) else ""
            out.write(f"  {k}: {typ}{req}{' - ' + p['description'] if p.get('description') else ''}\n")
        return 0
    if args.json:
        _show(tools, False, out)
        return 0
    w = max(len(t["name"]) for t in tools)
    for t in tools:
        first = t["description"].split(". ")[0].rstrip(".")
        out.write(f"{t['name']:<{w}}  {first}\n")
    return 0


def main_session(args, http=None, out=sys.stdout, err=sys.stderr) -> int:
    u, key = _client(args, http)
    if args.action == "show":
        name = key.split('|', 1)[1]
        out.write(f"{name}: {u.session or 'none'} ({u.where})" + (f", agent {u.agent}" if u.agent else "")
                  + f", in {_sessions_file()}\n")
        return 0
    if u.session:
        try:
            if u._l.token is None and not u._l.ensure_server():
                raise UndineError(u._l.problem)
            u.close()
        except (UndineError, OSError) as e:
            u.disconnect()
            err.write(f"undine: {e} (forgetting the session anyway)\n")
    with _sessions_held() as s:
        s.pop(key, None)
    out.write("session ended; a changeset it opened stays open for review, and `open_changeset id=...` continues it\n")
    return 0


def add_parsers(sub) -> None:
    """`undine call`, `undine tools`, `undine session` (in __main__.py's parser)."""
    def common(p):
        p.add_argument("--url", help=f"the Undine server (default $UNDINE_URL or {DEFAULT_URL})")
        p.add_argument("--ssh", metavar="HOST", help="Undine runs on HOST (user@host or an ~/.ssh/config alias; default "
                                                     "$UNDINE_SSH): reach it through an SSH port forward; --url is then as seen there")
        p.add_argument("--start", action="store_true", help="start Undine (with MCP on) if it is not running")
        p.add_argument("--session", help="the session kept between calls (default: default); an open changeset stays yours "
                                          "in it. A named session is the agent of that name (agent=NAME on every call "
                                          "that gives none): the history, the review and the write lock name it")
    c = sub.add_parser("call", help="call a tool (the assistant's and MCP clients') and print its JSON result",
                       description="Arguments: key=value (a string), key:=json (number, list, object, true ...), key=@path "
                                   "(the text of a file), key:=@path (the JSON in a file). `undine tools NAME` shows a "
                                   "tool's arguments. The request id goes to stderr first: --request-id sends the call "
                                   "again without acting twice.")
    c.add_argument("tool")
    c.add_argument("args", nargs="*", metavar="key=value")
    c.add_argument("--project", default=os.environ.get("UNDINE_PROJECT"), help="the project (default $UNDINE_PROJECT)")
    c.add_argument("--json", dest="json_args", metavar="OBJECT", help="arguments as one JSON object ('-' reads it from "
                                                                     "stdin, @FILE from a file)")
    c.add_argument("--raw", action="store_true", help="print the result on one line, as the tool returned it")
    c.add_argument("--max-chars", type=int, default=os.environ.get("UNDINE_MAX_CHARS") or SHELL_CHARS, metavar="N",
                   help=f"print at most N characters of the result, shortened inside and still valid JSON (default "
                        f"$UNDINE_MAX_CHARS or {SHELL_CHARS}; 0: all of it)")
    c.add_argument("--out", metavar="FILE.png", help="where a picture (render_*, plot_waveform) is written: a file, or a "
                                                    "directory for a new file in it (default: a new file in --out-dir; "
                                                    "the output gives its path)")
    c.add_argument("--out-dir", metavar="DIR", help="the directory a picture goes to when --out names no file (default "
                                                   "$UNDINE_OUT_DIR, else the folder of $UNDINE_CLI_SESSIONS when set, "
                                                   "else the current directory)")
    c.add_argument("--request-id", help="an id for this call (default: a new one): the same id again returns the first "
                                        "result instead of acting twice, e.g. when a script is re-run after a failure")
    common(c)
    t = sub.add_parser("tools", help="list the tools, or show one tool's arguments")
    t.add_argument("name", nargs="?")
    t.add_argument("--json", action="store_true", help="the tool schemas as JSON")
    common(t)
    s = sub.add_parser("session", help="show or end the session `undine call` keeps between calls")
    s.add_argument("action", choices=["show", "end"], nargs="?", default="show")
    common(s)


def main(args) -> None:
    raise SystemExit({"call": main_call, "tools": main_tools, "session": main_session}[args.cmd](args))
