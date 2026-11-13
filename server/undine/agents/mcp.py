"""MCP server (Streamable HTTP, JSON-RPC) exposing the same tools as the in-app assistant to external LLM clients.

Off unless the server is started with --mcp (and never when the LLM policy is `none`). Every request needs the bearer
token in <root>/mcp-token. Tools are `agent_tools`: reads are free, writes only inside a changeset's working copy,
there is no apply. What each tool returns leaves the server, so it is recorded in the project's llm/audit.jsonl
(connection kind "mcp") exactly like model calls are. Every tool takes `project` and `agent` (one of several agents on
one connection, Sessions), which are taken off the arguments before the tool runs; write tools take `request_id`.
A session survives a restart of the server (Sessions: its changesets, its lease owners), and so does a request id.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
from pathlib import Path

from undine.agents import agent_tools as T
from undine.server import idempotency
from undine.agents import skills

PROTOCOL = "2025-03-26"
INSTRUCTIONS = ("Undine is an AI-native, web-based EDA environment for custom IC design. Read the design with get_selection / get_design_context. "
                "To change something, open_changeset (or continue one: list_changesets, open_changeset with id), edit in it, "
                "run_checks, and tell the user to review it in Undine's review: you cannot apply a changeset. While you write, "
                "the changeset is yours alone; the user can stop you from Undine. Pass `project` (the real project name) to every tool; "
                "when several agents share this connection (subagents), each passes its own `agent` name to every tool, "
                "so that each has a changeset of its own. To verify another agent's changeset, pass its id as `changeset` "
                "to the read and check tools (run_checks, check_specs, extract_parasitics, get_layout_info ...) instead "
                "of opening it: that takes no write lock and leaves it theirs. "
                "Each cell has a design intent (goals, preferences, constraints, specs, context, with why; in get_design_context, "
                "in full with get_intent): work from it, never relax a must constraint or a spec on your own (propose it instead), "
                "and record what the user states as a lasting requirement or priority with add_intent, quoting their words. "
                "How to work, per task (schematic, layout, intent, verification, RTL): the resources undine://skills/undine/SKILL.md "
                "and its references; read only the one the task needs.")


def token_path(root: Path) -> Path:
    return root / "mcp-token"


def ensure_token(root: Path) -> str:
    p = token_path(root)
    if not p.is_file():
        p.write_text(secrets.token_urlsafe(32) + "\n")
        os.chmod(p, 0o600)
    return p.read_text().strip()


REQUEST_ID = {"type": "string", "description": "optional: an id you choose for this call (e.g. a UUID). Calling again with "
               "the same id (and the same `agent`) returns the first result instead of acting twice, so a retry after a "
               "timeout is safe; another agent's same id is another call"}
AGENT = {"type": "string", "description": "optional: your own name (e.g. layout-1) when several agents share this "
         "connection, such as a client's subagents: calls with the same name share a changeset (and its write lock), "
         "the history records the name with each edit, and another name's changeset is not yours to write. Without "
         "it, every call of the connection shares one changeset"}
AGENT_NAME = re.compile(r"[\w.-]{1,64}")      # an owner is "mcp:<session>:<agent>": no ':' in it (live.holder)
# Characters of one result for an MCP client (an LLM reads it, and keeps it in its context; the in-app assistant gets
# T.RESULT_CHARS). A longer one is shortened inside, valid JSON, saying what was left out and how to ask for it. A client
# chooses its own with params._meta["undine/max_chars"] (0: the whole result): the shell and Python clients ask for
# the whole one (`undine call` fits what it prints, --max-chars).
RESULT_CHARS = 20000
AUDIT_CHARS = 20000      # of a result in the audit log; a longer one is recorded by its start, length and hash
SESSIONS = "mcp-sessions.json"     # <root>/...: the sessions a restarted server resumes (Sessions)
KEEP_S = 30 * 86400                # a session unused this long is not resumed
TOUCH_S = 3600.0                   # how often a session in use says so in SESSIONS (its last use: KEEP_S counts from it)


def audited(text: str) -> dict:
    """A result as llm/audit.jsonl records it: whole, or (a client that asked for more) its first AUDIT_CHARS
    characters with its length and sha256, so the log does not grow by every long read."""
    if len(text) <= AUDIT_CHARS:
        return {"result": text}
    return {"result": text[:AUDIT_CHARS], "result_chars": len(text), "result_sha256": hashlib.sha256(text.encode()).hexdigest()}


def max_chars(params: dict) -> int | None:
    """The budget a tools/call asks for (None: the whole result), else RESULT_CHARS."""
    want = (params.get("_meta") or {}).get("undine/max_chars")
    if isinstance(want, bool) or not isinstance(want, (int, float)):
        return RESULT_CHARS
    return None if want <= 0 else max(int(want), 1000)


def tool_list() -> list[dict]:
    out = []
    for t in T.specs():
        schema = json.loads(json.dumps(t["input_schema"]))
        schema["properties"] = {"project": {"type": "string", "description": "the Undine project (real name, not a preview)"},
                                **schema["properties"]}
        if T.BY_NAME[t["name"]]["write"]:
            schema["properties"]["request_id"] = REQUEST_ID
        schema["properties"]["agent"] = AGENT
        schema["required"] = ["project", *schema.get("required", [])]
        out.append({"name": t["name"], "description": t["description"], "inputSchema": schema})
    return out


class Sessions:
    """The sessions' state: per agent and project the ToolCtx (the changeset it writes). Calls run in worker threads,
    several at once, also from one session (a client sends tool calls in parallel).

    An agent is a name a call gives (`agent`): several agents can share one MCP connection (a desktop client's
    subagents over one mcp-stdio relay), each with a changeset of its own. Each is a writer of its own (lease owner
    "mcp:<session>:<agent>", so one never writes the changeset another holds) and an actor of its own in the history
    ("agent:<client>/<agent>"). Calls without a name are the connection's own agent ("mcp:<session>",
    "agent:<client>").

    A server's sessions (`srv`) outlive it: <root>/SESSIONS keeps each session that made a call (its client, and per
    agent and project the changeset open in it) and the writers that were stopped, so that after a restart (to deploy
    a change) the same session id goes on with the same changesets, the same lease owners and the jobs it started,
    and a stopped agent stays stopped. A session unused for KEEP_S is forgotten: its client starts a new one (MCP's
    404), and the shell client opens its changeset again itself (client.py). One server at a time writes SESSIONS: the
    one that keeps the root's jobs (sim.Jobs.keeper). The next one, started while the last one still finishes its calls,
    reads it again when it changes and for a session it does not know, keeps what it changes meanwhile (dirty), and
    writes it once it holds the root (_sync). Where no server kept sessions yet (the first start of this version), the
    sessions whose jobs still run in a changeset are taken from those jobs (_seed)."""

    def __init__(self, srv=None):
        self.s: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.srv = srv
        self.path = srv.store.root / SESSIONS if srv is not None else None
        self._writing = threading.RLock()
        self.dirty: set[str] = set()           # sessions changed here while another server writes SESSIONS ...
        self.ended: set[str] = set()           # ... and ended here meanwhile
        self.seen = None                       # SESSIONS as last read or written: (mtime_ns, size)
        if self.path is not None:
            jobs = getattr(srv, "jobs", None)
            if jobs is not None:
                jobs.on_owner = self._sync
            with self._writing:
                if self.path.exists():
                    self._load()
                else:
                    self._seed()
            srv.leases.on_stop = self.save

    def new(self, client: str) -> str:
        sid = uuid.uuid4().hex
        with self.lock:
            self.s[sid] = {"client": client, "ctx": {}, "t": time.time()}
        return sid

    def drop(self, sid: str) -> None:
        with self.lock:
            gone = self.s.pop(sid, None)
            self.ended.add(sid)
            self.dirty.discard(sid)
        if gone is not None and gone["ctx"]:
            self.save()

    def knows(self, sid: str) -> bool:
        """Whether this server has the session, also one the server still stopping started (SESSIONS read again)."""
        if sid not in self.s:
            self._refresh()
        return sid in self.s

    def ctx(self, srv, sid: str, project: str, agent: str | None = None, own: bool = False) -> T.ToolCtx:
        """The context of `agent` (None: the session's unnamed one) in `project`. `own`: the agent is the session's own
        name (a named `undine call --session NAME`): when it is new in the session, it takes over the changeset the
        session's unnamed agent has open in the project, with its write lock (a stopped one stays stopped), as a
        session named before its name was its agent goes on under its name. The shell client says so once only, at the
        first call of a session an earlier client made (client._client): a changeset the unnamed agent opens later
        (agent="") stays its own."""
        self._refresh()
        now = time.time()
        with self.lock:
            sess = self.s.setdefault(sid, {"client": "mcp", "ctx": {}})
            sess["t"] = now                             # its last use (KEEP_S)
            key = (agent or "", project)
            c = sess["ctx"].get(key)
            if c is None:
                c = sess["ctx"][key] = _ctx(srv, sid, sess["client"], project, agent)
                old = sess["ctx"].get(("", project)) if own and agent else None
                if old is not None and old.changeset:
                    c.changeset, old.changeset = old.changeset, None
                    srv.leases.hand_over(c.changeset, old.owner, c.owner, c.actor.removeprefix("agent:"))
                    if old.owner in srv.leases.stopped():      # kept in SESSIONS by the save below
                        srv.leases.restore_stopped([c.owner])
                self.dirty.add(sid)
            elif now - sess.get("kept", 0.0) < TOUCH_S:
                return c
        self.save()                       # who it is (the owner of its jobs) is kept from now on, and that it is in use
        return c

    def call(self, shared: T.ToolCtx, name: str, args: dict, rid, project_dir: Path,
             limit: int | None = RESULT_CHARS, agent: str | None = None) -> tuple[str, bool, bool, T.ToolCtx]:
        """Runs one tool call on a copy of the session's context, so that calls running at once keep their own pictures
        and records; what a call changes for the session (the changeset it opened, its edits) is kept. `limit`: the
        result's budget in characters (max_chars; None: whole); `agent`: the caller's name, whose request ids are its
        own (idempotency.once). Returns (text, is_error, replayed, the call's context)."""
        ctx = dataclasses.replace(shared, images=[], committed=[])
        opened, writes = ctx.changeset, ctx.writes
        replayed = False
        if rid is not None and name in T.BY_NAME and T.BY_NAME[name]["write"]:     # a retry of a write never acts twice
            text, err, replayed = idempotency.once(project_dir, rid, name, args, lambda: T.call(ctx, name, args, limit),
                                                   agent)
        else:
            text, err = T.call(ctx, name, args, limit)
        with self.lock:
            moved = ctx.changeset != opened and shared.changeset != ctx.changeset
            if ctx.changeset != opened:
                shared.changeset = ctx.changeset
            shared.writes += ctx.writes - writes
            if moved and shared.owner:
                self.dirty.add(shared.owner.split(":")[1])
        if moved:
            self.save()
        return text, err, replayed, ctx

    # ------------------------------------------------------------------ across restarts

    def _keeper(self) -> bool:
        jobs = getattr(self.srv, "jobs", None)
        return getattr(jobs, "keeper", True)

    def save(self) -> None:
        """Writes SESSIONS (the sessions that made a call, the stopped writers), when the server's sessions are kept and
        this server keeps them now (sim.Jobs.keeper; else what changed is written once it does: _sync)."""
        if self.path is None or not self._keeper():
            return
        with self._writing:                     # in order: a later picture is never overwritten by an earlier one
            now = time.time()
            with self.lock:
                kept = {sid: {"client": sess["client"], "t": sess.get("t") or now,
                              "ctx": [{"agent": a, "project": p, "changeset": c.changeset}
                                      for (a, p), c in sess["ctx"].items()]}
                        for sid, sess in self.s.items() if sess["ctx"]}
                for sid in kept:
                    self.s[sid]["kept"] = now
            stopped = sorted(o for o in self.srv.leases.stopped() if o.startswith("mcp:"))
            try:
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps({"sessions": kept, "stopped": stopped}))
                os.chmod(tmp, 0o600)
                tmp.replace(self.path)
                self.seen = _stamp(self.path)
                with self.lock:
                    self.dirty.clear()
                    self.ended.clear()
            except OSError as e:
                print(f"undine: cannot keep the MCP sessions in {self.path}: {e}", file=sys.stderr)

    def _refresh(self) -> None:
        """While another server writes SESSIONS (the last one still finishing its calls): what it wrote since it was
        read, for the sessions this one has not changed meanwhile."""
        if self.path is not None and not self._keeper() and _stamp(self.path) != self.seen:
            with self._writing:
                self._load()

    def _sync(self) -> None:
        """This server keeps the root now (the last one has gone): SESSIONS as the last one left it, with what this one
        changed meanwhile, written."""
        with self._writing:
            self._load()
            self.save()

    def _load(self) -> None:
        """The sessions SESSIONS keeps (save), but those unused for KEEP_S and those this server changed or ended since
        it last read it (dirty, ended); a session it has is brought up to date in place (calls may hold its contexts)."""
        self.seen = _stamp(self.path)
        try:
            data = json.loads(self.path.read_text())
            sessions, stopped = dict(data.get("sessions") or {}), list(data.get("stopped") or [])
        except (OSError, ValueError, AttributeError, TypeError):
            return
        now = time.time()
        for sid, rec in sessions.items():
            try:
                if now - float(rec.get("t") or 0) > KEEP_S:
                    continue
                client = str(rec.get("client") or "mcp")
                ctx = {}
                for e in rec.get("ctx") or []:
                    agent, project, cid = e.get("agent") or "", str(e["project"]), e.get("changeset")
                    ctx[(agent, project)] = (agent or None, cid if isinstance(cid, str) else None)
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            self._put(sid, client, ctx, float(rec.get("t") or now))
        self.srv.leases.restore_stopped(o for o in stopped if isinstance(o, str)
                                        and o.removeprefix("mcp:").partition(":")[0] in self.s)

    def _put(self, sid: str, client: str, ctx: dict, t: float) -> None:
        """A session as it was kept ({(agent, project): (agent, changeset)}), unless this server changed or ended it
        since it last read SESSIONS."""
        with self.lock:
            if sid in self.dirty or sid in self.ended:
                return
            sess = self.s.setdefault(sid, {"client": client, "ctx": {}})
            sess.update(t=max(t, sess.get("t") or 0.0), kept=time.time())
            for (agent, project), (name, cid) in ctx.items():
                if (c := sess["ctx"].get((agent, project))) is not None:
                    c.changeset = cid
                else:
                    sess["ctx"][(agent, project)] = _ctx(self.srv, sid, sess["client"], project, name, cid)

    def _seed(self) -> None:
        """No server kept sessions on this root yet (an Undine before this version ran it): the sessions whose
        simulations still run (or wait) in a changeset's working copy, from those jobs (their owner "mcp:<session>" or
        "mcp:<session>:<agent>"), with that changeset open in them, so that their agents go on with it after this first
        restart. Read once; SESSIONS is written at the first save."""
        base = self.srv.store.root / ".changesets"
        found: dict[str, dict] = {}
        for st_file in base.glob("*/cs-*/work/runs/*/status.json"):
            try:
                st = json.loads(st_file.read_text())
            except (OSError, ValueError):
                continue
            owner = st.get("owner") if isinstance(st, dict) else None
            if st.get("state") not in ("queued", "running") or not isinstance(owner, str) \
                    or not owner.startswith("mcp:") or st.get("reader"):     # a reader's: not its changeset
                continue
            sid, _, agent = owner[4:].partition(":")
            project, cid = st_file.parents[4].name, st_file.parents[3].name
            try:
                meta = json.loads((self.srv.store.root / project / "changesets" / f"{cid}.json").read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict) or meta.get("status") != "open" or not re.fullmatch(r"[0-9a-f]{32}", sid):
                continue
            who = str(meta.get("actor") or "").removeprefix("agent:")
            client = who[:-len(agent) - 1] if agent and who.endswith(f"/{agent}") else who if not agent else "mcp"
            rec = found.setdefault(sid, {"client": client or "mcp", "ctx": {}})
            rec["ctx"][(agent, project)] = (agent or None, cid)
        for sid, rec in found.items():
            self._put(sid, rec["client"], rec["ctx"], time.time())
        if found:
            print(f"undine: MCP sessions taken from the jobs they run: {len(found)} ("
                  + ", ".join(f"{sid[:8]}: {', '.join(c for _, c in r['ctx'].values())}" for sid, r in found.items())
                  + ")", file=sys.stderr)


def _stamp(p: Path):
    try:
        st = p.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def _ctx(srv, sid: str, client: str, project: str, agent: str | None, changeset: str | None = None) -> T.ToolCtx:
    """An agent's context in a session: its actor in the history, its lease owner, the changeset it writes."""
    who = f"{client}/{agent}" if agent else client
    return T.ToolCtx(srv, project, actor=f"agent:{who}", changeset=changeset,
                     owner=f"mcp:{sid}:{agent}" if agent else f"mcp:{sid}", owner_kind="mcp")


def _rpc_error(i, code, msg):
    return {"jsonrpc": "2.0", "id": i, "error": {"code": code, "message": msg}}


def handle(srv, sessions: Sessions, body, headers: dict) -> tuple[int, dict | None, dict]:
    """One JSON-RPC message -> (HTTP status, body, extra headers). The server runs it in a worker thread, several at
    once."""
    auth = headers.get("authorization", "")
    expected = ensure_token(srv.store.root)
    if not auth.lower().startswith("bearer ") or not hmac.compare_digest(auth[7:].strip(), expected):
        return 401, {"error": "missing or wrong bearer token (see <projects root>/mcp-token)"}, {"www-authenticate": "Bearer"}
    if not isinstance(body, dict):
        return 400, _rpc_error(None, -32600, "expected a single JSON-RPC message"), {}
    m, i, p = body.get("method"), body.get("id"), body.get("params") or {}
    sid = headers.get("mcp-session-id") or ""
    if sid and m != "initialize" and not sessions.knows(sid):
        # a session this server does not know (ended, or forgotten: Sessions.KEEP_S; one a restarted server kept goes
        # on): MCP's answer is 404, and the client starts a new session
        return 404, {"error": "unknown MCP session; send initialize again"}, {}
    if i is None:  # notification (e.g. notifications/initialized)
        return 202, None, {}
    if m == "initialize":
        client = (p.get("clientInfo") or {}).get("name", "mcp")
        sid = sessions.new(client)
        return 200, {"jsonrpc": "2.0", "id": i, "result": {
            "protocolVersion": p.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
            "serverInfo": {"name": "undine", "version": "0.1"}, "instructions": INSTRUCTIONS}}, {"mcp-session-id": sid}
    if m == "ping":
        return 200, {"jsonrpc": "2.0", "id": i, "result": {}}, {}
    if m == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": i, "result": {"tools": tool_list()}}, {}
    if m == "resources/list":                      # the agent guide (skills.py), read per task
        return 200, {"jsonrpc": "2.0", "id": i, "result": {"resources": skills.resources()}}, {}
    if m == "resources/read":
        uri = p.get("uri", "")
        try:
            text = skills.read(uri)
        except KeyError as e:
            return 200, _rpc_error(i, -32002, str(e).strip("'\"")), {}
        return 200, {"jsonrpc": "2.0", "id": i, "result": {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]}}, {}
    if m == "tools/call":
        name, args = p.get("name"), dict(p.get("arguments") or {})
        project = args.pop("project", None)
        rid = args.pop("request_id", None)
        agent = args.pop("agent", None)                  # one of several agents on this connection (Sessions)
        agent = (agent.strip() or None) if isinstance(agent, str) else agent
        if not project:
            return 200, {"jsonrpc": "2.0", "id": i, "result": {"isError": True, "content": [{"type": "text", "text": "project is required"}]}}, {}
        if agent is not None and not (isinstance(agent, str) and AGENT_NAME.fullmatch(agent)):
            return 200, {"jsonrpc": "2.0", "id": i, "result": {"isError": True, "content": [{"type": "text", "text":
                         f"agent is a name of up to 64 letters, digits, '.', '_' or '-' (e.g. layout-1), not {agent!r}"}]}}, {}
        try:
            srv.project(project)
            if "~" in project:
                raise KeyError("pass the real project name, not a changeset preview")
        except KeyError as e:
            return 200, {"jsonrpc": "2.0", "id": i, "result": {"isError": True, "content": [{"type": "text", "text": str(e).strip("'\"")}]}}, {}
        own = bool((p.get("_meta") or {}).get("undine/own_agent"))      # a named shell session's own name
        text, err, replayed, ctx = sessions.call(sessions.ctx(srv, sid, project, agent, own), name, args, rid,
                                                 srv.project(project).path, max_chars(p), agent)
        client = sessions.s.get(sid, {}).get("client", "mcp")
        srv.gateway.audit_event(srv.project(project).path, f"mcp:{name}", connection={"kind": "mcp", "model": client, "endpoint": "mcp"},
                                payload={"arguments": args, "session": sid[:8], **({"agent": agent} if agent else {}),
                                         **audited(text),
                                         **({"request_id": rid, "replayed": replayed} if rid else {}),
                                         **({"images": len(ctx.images)} if ctx.images else {})},
                                ok=not err)
        images = [] if err else [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()}
                                 for png in ctx.images]           # a picture (render_*, plot_waveform) beside its numbers
        result = {"content": [{"type": "text", "text": text}, *images], "isError": err}
        if replayed:
            result["_meta"] = {"undine/replayed": True}    # the first call's result: this call did nothing
        return 200, {"jsonrpc": "2.0", "id": i, "result": result}, {}
    return 200, _rpc_error(i, -32601, f"method not found: {m}"), {}
