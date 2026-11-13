"""LLM gateway: one exclusive connection, enforced on the server.

Every LLM call of Undine (chat answers now; explanations, summaries and change proposals later) goes through
`Gateway.complete`. It is the only place that talks to a model, so these rules cannot be bypassed from the browser
or by another feature:

* There is exactly ONE active connection: `cloud` (Anthropic API) or `server` (an OpenAI-compatible endpoint that runs
  next to the EDA server, e.g. Ollama / vLLM). It is stored in <root>/llm.json and changed only by an explicit switch.
* The server policy (`--llm-policy` / $UNDINE_LLM_POLICY = both | cloud | server | none) can forbid a kind outright;
  with `server` no code path ever creates a cloud client.
* No automatic fallback: if the connection fails the error is returned; nothing is re-sent to another endpoint.
* API keys live on the server: in its environment, or registered from the UI (sent once, kept in
  ~/.undine/connections.json readable only by the user, see `Connections`). They are never stored in a project, never
  written to the audit log and never sent back to the browser (only the last four characters are shown).
* Everything sent is written to <project>/llm/audit.jsonl (connection, purpose, the exact messages).
* A conversation is bound to the connection it started on. Continuing it on another connection needs an
  explicit "carry the history over" action (the history is then sent to the new endpoint) or a new conversation.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from undine import home as undine_home

POLICIES = ("both", "cloud", "server", "none")
KINDS = ("cloud", "server")
DEFAULT_CLOUD_MODEL = "claude-haiku-5-5"
DEFAULT_SERVER_URL = "http://127.0.0.1:11434/v1"
MAX_TOKENS = 16000


IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")
MAX_IMAGES = 2
MAX_IMAGE_BYTES = 3 * 1024 * 1024


def _oa_content(content):
    """Anthropic-style content (str or blocks) -> OpenAI-compatible message content."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if b["type"] == "text":
            parts.append({"type": "text", "text": b["text"]})
        elif b["type"] == "image":
            src = b["source"]
            parts.append({"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
    return parts


def _home() -> Path:
    return undine_home.home()


def _private_host(url: str) -> bool:
    import ipaddress
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost",) or host.endswith((".local", ".localhost")):
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


PROVIDERS = {"anthropic": {"base_url": "https://api.anthropic.com"}, "openai": {"base_url": "https://api.openai.com/v1"}}


class Connections:
    """LLM connections registered from the UI: provider, endpoint, API key, models. One file for the user, mode 0600.
    `where` says where the data goes, for the policy: `server` for an endpoint on this machine or a private network,
    `cloud` otherwise."""

    def __init__(self, home: Path | None = None):
        self.file = (home or _home()) / "connections.json"
        self.lock = threading.RLock()

    def _load(self) -> list[dict]:
        try:
            return json.loads(self.file.read_text()).get("connections", [])
        except (OSError, ValueError):
            return []

    def _save(self, rows: list[dict]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"connections": rows}, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(self.file)

    def get(self, cid: str) -> dict:
        r = next((r for r in self._load() if r["id"] == cid), None)
        if r is None:
            raise KeyError(f"no LLM connection {cid}")
        return r

    def add(self, label: str, provider: str, api_key: str, base_url: str | None, models: list[str]) -> dict:
        if provider not in PROVIDERS:
            raise ValueError(f"provider must be one of {', '.join(PROVIDERS)}")
        url = (base_url or PROVIDERS[provider]["base_url"]).rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        where = "server" if _private_host(url) else "cloud"
        if not api_key and where == "cloud":
            raise ValueError("an API key is required for a cloud endpoint")
        rec = {"id": "c-" + uuid.uuid4().hex[:8], "label": label.strip() or provider, "provider": provider, "base_url": url,
               "where": where, "key": api_key.strip(), "models": [m for m in models if m], "added": _now()}
        with self.lock:
            self._save([*self._load(), rec])
        return rec

    def remove(self, cid: str) -> None:
        with self.lock:
            self._save([r for r in self._load() if r["id"] != cid])

    def set_models(self, cid: str, models: list[str]) -> None:
        with self.lock:
            rows = self._load()
            for r in rows:
                if r["id"] == cid:
                    r["models"] = models
            self._save(rows)

    @staticmethod
    def public(r: dict) -> dict:
        return {k: r[k] for k in ("id", "label", "provider", "base_url", "where", "models", "added")} | \
            {"key_hint": ("…" + r["key"][-4:]) if r.get("key") else None}

    def list(self) -> list[dict]:
        return [self.public(r) for r in self._load()]


def list_models(provider: str, base_url: str, key: str | None) -> list[str]:
    """The models an endpoint offers (Anthropic /v1/models, OpenAI-compatible /models): also checks the key."""
    if provider == "anthropic":
        req = urllib.request.Request(base_url + "/v1/models?limit=100", headers={"x-api-key": key or "", "anthropic-version": "2023-06-01"})
    else:
        req = urllib.request.Request(base_url + "/models", headers={"authorization": f"Bearer {key}"} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r).get("data") or []
    except urllib.error.HTTPError as e:
        raise LlmError(f"{provider} answered {e.code}: {e.read().decode(errors='replace')[:200]}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LlmError(f"cannot reach {base_url}: {e}") from e
    return [m["id"] for m in data if m.get("id")]


class LlmError(Exception):
    """The connection failed or refused; shown to the user as is (never retried elsewhere)."""


class LlmPolicyError(LlmError):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Gateway:
    def __init__(self, root: Path, policy: str | None = None):
        self.root = root
        self.policy = (policy or os.environ.get("UNDINE_LLM_POLICY") or "both").lower()
        if self.policy not in POLICIES:
            raise ValueError(f"--llm-policy must be one of {', '.join(POLICIES)}")
        self.lock = threading.RLock()
        self.conns = Connections()

    # ------------------------------------------------------------ configuration

    def allowed(self) -> list[str]:
        return [k for k in KINDS if self.policy in ("both", k)]

    def _file(self) -> Path:
        return self.root / "llm.json"

    def active(self) -> dict:
        conf: dict = {}
        try:
            conf = json.loads(self._file().read_text())
        except Exception:
            pass
        if conf.get("connection"):
            try:
                r = self.conns.get(conf["connection"])
                return {"kind": r["where"], "provider": r["provider"], "model": conf.get("model") or (r["models"] or [""])[0],
                        "base_url": r["base_url"], "connection": r["id"], "connection_label": r["label"]}
            except KeyError:
                conf = {}                                    # removed since: fall back to the built-in connection
        kind = conf.get("kind") if conf.get("kind") in KINDS else (self.allowed() or ["cloud"])[0]
        if kind == "cloud":
            return {"kind": "cloud", "model": conf.get("model") if conf.get("kind") == "cloud" and conf.get("model") else DEFAULT_CLOUD_MODEL,
                    "base_url": None}
        return {"kind": "server", "model": conf.get("model", "") if conf.get("kind") == "server" else "",
                "base_url": (conf.get("base_url") if conf.get("kind") == "server" else None)
                or os.environ.get("UNDINE_LLM_SERVER_URL") or DEFAULT_SERVER_URL}

    def status(self) -> dict:
        a = self.active()
        return {
            "policy": self.policy, "allowed": self.allowed(), "active": a, "connections": self.conns.list(),
            "label": self.label(a),
            "ready": a["kind"] in self.allowed() and (a["kind"] == "cloud" or bool(a["model"])),
            "cloud_key_set": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
            "server_url_default": os.environ.get("UNDINE_LLM_SERVER_URL") or DEFAULT_SERVER_URL,
        }

    @staticmethod
    def label(a: dict) -> str:
        if a.get("connection"):
            return f"{a['connection_label']} · {a['model'] or '(no model set)'}"
        return f"Cloud · {a['model']}" if a["kind"] == "cloud" else f"EDA server · {a['model'] or '(no model set)'}"

    def switch(self, kind: str, model: str | None, base_url: str | None, actor: str = "user",
               connection: str | None = None) -> dict:
        """The explicit way to change the connection. Policy is enforced here as well as at call time."""
        if connection:
            r = self.conns.get(connection)
            if r["where"] not in self.allowed():
                raise LlmPolicyError(f"the server policy ({self.policy}) does not allow {r['where']} connections")
            if not (model or r["models"]):
                raise LlmError("choose a model for this connection")
            with self.lock:
                tmp = self._file().with_suffix(".tmp")
                tmp.write_text(json.dumps({"connection": r["id"], "model": model or r["models"][0], "changed": _now(), "by": actor},
                                          indent=2) + "\n")
                tmp.replace(self._file())
            return self.status()
        if kind not in KINDS:
            raise ValueError(f"unknown connection kind {kind}")  # a bad request, not a failed connection
        if kind not in self.allowed():
            raise LlmPolicyError(f"the server policy ({self.policy}) does not allow {kind} connections")
        if kind == "cloud":
            conf = {"kind": "cloud", "model": model or DEFAULT_CLOUD_MODEL}
        else:
            if not model:
                raise LlmError("a model name is required for the EDA-server connection")
            conf = {"kind": "server", "model": model, "base_url": (base_url or DEFAULT_SERVER_URL).rstrip("/")}
            self._check_url(conf["base_url"])
        with self.lock:
            tmp = self._file().with_suffix(".tmp")
            tmp.write_text(json.dumps({**conf, "changed": _now(), "by": actor}, indent=2) + "\n")
            tmp.replace(self._file())
        return self.status()

    @staticmethod
    def _check_url(url: str) -> None:
        if not url.startswith(("http://", "https://")):
            raise LlmError("base_url must be an http(s) URL")

    # ------------------------------------------------------------ the one choke point

    def complete(self, project_path: Path, purpose: str, system: str, messages: list[dict],
                 conversation: str | None = None, connection: dict | None = None, summary: dict | None = None) -> dict:
        """Sends one request to the active connection. `connection`, when given, must equal the active one
        (a conversation bound to another connection is refused here, not silently moved)."""
        a = self.active()
        try:
            self._guard(a, connection)
        except LlmError as e:
            self.audit_event(project_path, "refused", conversation=conversation, reason=str(e), connection={"kind": a["kind"], "model": a["model"]})
            raise
        return self._send(project_path, purpose, system, messages, conversation, a, summary)

    def _guard(self, a: dict, connection: dict | None) -> None:
        if connection and (connection["kind"], connection["model"]) != (a["kind"], a["model"]):
            raise LlmError(f"this conversation belongs to {connection['kind']} / {connection['model']}, but the active "
                           f"connection is {a['kind']} / {a['model']}. Start a new conversation or continue it on the "
                           "active connection explicitly.")
        if a["kind"] not in self.allowed():
            raise LlmPolicyError(f"the server policy ({self.policy}) does not allow {a['kind']} connections")
        if not a["model"] and (a["kind"] == "server" or a.get("connection")):
            raise LlmError("no model is set for this connection")

    def _send(self, project_path: Path, purpose: str, system: str, messages: list[dict], conversation: str | None,
              a: dict, summary: dict | None) -> dict:
        entry = {"t": _now(), "purpose": purpose, "conversation": conversation, "connection": {"kind": a["kind"], "model": a["model"],
                 "endpoint": a["base_url"] or "api.anthropic.com"}, "summary": summary or {},
                 "payload": {"system": system, "messages": self._audit_messages(project_path, messages)}}
        t0 = time.time()
        try:
            out = self._cloud(a, system, messages) if self._provider(a) == "anthropic" else self._server(a, system, messages)
        except LlmError as e:
            self._audit(project_path, {**entry, "ok": False, "error": str(e), "seconds": round(time.time() - t0, 2)})
            raise
        self._audit(project_path, {**entry, "ok": True, "usage": out.get("usage"), "stop": out.get("stop"),
                                   "seconds": round(time.time() - t0, 2)})
        return {**out, "connection": {"kind": a["kind"], "model": a["model"]}}

    def complete_tools(self, project_path: Path, purpose: str, system: str, messages: list[dict], tools: list[dict],
                       conversation: str | None = None, connection: dict | None = None, summary: dict | None = None,
                       step: int = 0) -> dict:
        """Like `complete`, with tools. Messages are Anthropic-style (content blocks incl. tool_use / tool_result);
        the server connection converts them to / from the OpenAI format. Returns {"blocks": [...], "stop": ...}."""
        a = self.active()
        try:
            self._guard(a, connection)
        except LlmError as e:
            self.audit_event(project_path, "refused", conversation=conversation, reason=str(e), connection={"kind": a["kind"], "model": a["model"]})
            raise
        t0 = time.time()
        entry = {"t": _now(), "purpose": purpose, "step": step, "conversation": conversation,
                 "connection": {"kind": a["kind"], "model": a["model"], "endpoint": a["base_url"] or "api.anthropic.com"},
                 "summary": {**(summary or {}), "tools": [t["name"] for t in tools]},
                 "payload": {"system": system if step == 0 else "(unchanged)",
                             "messages": self._audit_messages(project_path, messages if step == 0 else messages[-1:])}}
        try:
            out = (self._cloud_tools(system, messages, tools, a) if self._provider(a) == "anthropic"
                   else self._server_tools(a, system, messages, tools))
        except LlmError as e:
            self._audit(project_path, {**entry, "ok": False, "error": str(e), "seconds": round(time.time() - t0, 2)})
            raise
        self._audit(project_path, {**entry, "ok": True, "usage": out.get("usage"), "stop": out.get("stop"),
                                   "seconds": round(time.time() - t0, 2),
                                   "response": [b if b["type"] != "thinking" else {"type": "thinking"} for b in out["blocks"]]})
        return {**out, "connection": {"kind": a["kind"], "model": a["model"]}}

    @staticmethod
    def _provider(a: dict) -> str:
        return a.get("provider") or ("anthropic" if a["kind"] == "cloud" else "openai")

    def _key(self, a: dict) -> str | None:
        """A registered connection's key; None: the environment's (ANTHROPIC_API_KEY / UNDINE_LLM_SERVER_KEY)."""
        return self.conns.get(a["connection"]).get("key") or None if a.get("connection") else None

    def _anthropic(self, a: dict):
        import anthropic
        if a.get("connection"):
            return anthropic.Anthropic(api_key=self._key(a), base_url=a["base_url"])
        return anthropic.Anthropic()

    def _cloud_tools(self, system, messages, tools, a) -> dict:
        import anthropic

        try:
            r = self._anthropic(a).messages.create(model=a["model"], max_tokens=MAX_TOKENS, system=system,
                                                      messages=messages, tools=tools)
        except anthropic.APIStatusError as e:
            raise LlmError(f"Anthropic API error {e.status_code}: {getattr(e, 'message', e)}") from e
        except anthropic.APIConnectionError as e:
            raise LlmError(f"cannot reach the Anthropic API: {e}") from e
        except Exception as e:
            if "Could not resolve authentication" in str(e):
                raise LlmError("The EDA server has no Anthropic credentials. Set ANTHROPIC_API_KEY in the environment of the "
                               "server process (e.g. in ~/.undine/env, see README) and restart it, or add a connection with its "
                               "key in the Assistant's connection settings.") from e
            raise LlmError(f"cloud connection failed: {e}") from e
        blocks = [b.model_dump(exclude_none=True) for b in r.content]  # thinking blocks are echoed back unchanged
        return {"blocks": blocks, "stop": r.stop_reason, "usage": {"input": r.usage.input_tokens, "output": r.usage.output_tokens}}

    def _server_tools(self, a, system, messages, tools) -> dict:
        oa = [{"role": "system", "content": system}]
        for m in messages:
            if isinstance(m["content"], str):
                oa.append({"role": m["role"], "content": m["content"]})
            elif m["role"] == "user" and any(b["type"] == "image" for b in m["content"]):
                oa.append({"role": "user", "content": _oa_content(m["content"])})
            elif m["role"] == "assistant":
                text = "".join(b["text"] for b in m["content"] if b["type"] == "text")
                calls = [{"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                         for b in m["content"] if b["type"] == "tool_use"]
                oa.append({"role": "assistant", "content": text or None, **({"tool_calls": calls} if calls else {})})
            else:
                for b in m["content"]:
                    if b["type"] == "tool_result":
                        oa.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]})
                    elif b["type"] == "text":
                        oa.append({"role": "user", "content": b["text"]})
        body = json.dumps({"model": a["model"], "max_tokens": MAX_TOKENS, "stream": False, "messages": oa,
                           "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                                      "parameters": t["input_schema"]}} for t in tools]}).encode()
        d = self._post_openai(a, body)
        try:
            ch = d["choices"][0]
            msg = ch["message"]
            blocks = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg.get("tool_calls") or []:
                try:
                    inp = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    inp = {}
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": inp})
            u = d.get("usage") or {}
            stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else (ch.get("finish_reason") or "end_turn")
            return {"blocks": blocks, "stop": stop, "usage": {"input": u.get("prompt_tokens"), "output": u.get("completion_tokens")}}
        except (KeyError, IndexError, TypeError) as e:
            raise LlmError(f"unexpected response from the EDA-server LLM: {str(d)[:200]}") from e

    def _post_openai(self, a: dict, body: bytes) -> dict:
        req = urllib.request.Request(a["base_url"] + "/chat/completions", data=body, method="POST",
                                     headers={"content-type": "application/json"})
        key = self._key(a) if a.get("connection") else os.environ.get("UNDINE_LLM_SERVER_KEY")
        if key:
            req.add_header("authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise LlmError(f"EDA-server LLM error {e.code}: {e.read().decode(errors='replace')[:300]}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LlmError(f"cannot reach the EDA-server LLM at {a['base_url']}: {e}") from e

    def audit_event(self, project_path: Path, event: str, **kw) -> None:
        self._audit(project_path, {"t": _now(), "purpose": event, **kw})

    @staticmethod
    def image_dir(project_path: Path) -> Path:
        return project_path / "llm" / "images"

    def _audit_messages(self, project_path: Path, messages: list[dict]) -> list[dict]:
        """Messages as recorded in the audit log: images are stored once as files (by hash) and referenced, so the exact
        pixels that were sent can be reviewed without bloating the log."""
        import base64
        import hashlib

        out = []
        for m in messages:
            if isinstance(m["content"], str):
                out.append(m)
                continue
            blocks = []
            for b in m["content"]:
                if b.get("type") == "image":
                    raw = base64.b64decode(b["source"]["data"])
                    sha = hashlib.sha1(raw).hexdigest()
                    d = self.image_dir(project_path)
                    d.mkdir(parents=True, exist_ok=True)
                    ext = b["source"]["media_type"].split("/")[1]
                    (d / f"{sha}.{ext}").write_bytes(raw) if not (d / f"{sha}.{ext}").exists() else None
                    blocks.append({"type": "image", "sha1": sha, "media_type": b["source"]["media_type"], "bytes": len(raw)})
                else:
                    blocks.append(b)
            out.append({**m, "content": blocks})
        return out

    def _audit(self, project_path: Path, rec: dict) -> None:
        with self.lock:
            d = project_path / "llm"
            d.mkdir(exist_ok=True)
            with (d / "audit.jsonl").open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")

    # ------------------------------------------------------------ transports

    def _cloud(self, a: dict, system: str, messages: list[dict]) -> dict:
        import anthropic  # imported only when a cloud call is actually made (and allowed)

        try:
            r = self._anthropic(a).messages.create(model=a["model"], max_tokens=MAX_TOKENS, system=system,
                                                   messages=messages)
        except anthropic.APIStatusError as e:
            raise LlmError(f"Anthropic API error {e.status_code}: {getattr(e, 'message', e)}") from e
        except anthropic.APIConnectionError as e:
            raise LlmError(f"cannot reach the Anthropic API: {e}") from e
        except Exception as e:
            if "Could not resolve authentication" in str(e):
                raise LlmError("The EDA server has no Anthropic credentials. Set ANTHROPIC_API_KEY in the environment of the "
                               "server process (e.g. in ~/.undine/env, see README) and restart it, or add a connection with its "
                               "key in the Assistant's connection settings.") from e
            raise LlmError(f"cloud connection failed: {e}") from e
        text = "".join(b.text for b in r.content if b.type == "text")
        stop = r.stop_reason
        if stop == "refusal":
            det = getattr(r, "stop_details", None)
            text = text or f"The model declined this request ({getattr(det, 'category', None) or 'policy'})."
        return {"text": text, "stop": stop, "usage": {"input": r.usage.input_tokens, "output": r.usage.output_tokens}}

    def _server(self, a: dict, system: str, messages: list[dict]) -> dict:
        body = json.dumps({"model": a["model"], "max_tokens": MAX_TOKENS, "stream": False,
                           "messages": [{"role": "system", "content": system},
                                        *({**m, "content": _oa_content(m["content"])} for m in messages)]}).encode()
        d = self._post_openai(a, body)
        try:
            ch = d["choices"][0]
            u = d.get("usage") or {}
            return {"text": ch["message"]["content"] or "", "stop": ch.get("finish_reason"),
                    "usage": {"input": u.get("prompt_tokens"), "output": u.get("completion_tokens")}}
        except (KeyError, IndexError, TypeError) as e:
            raise LlmError(f"unexpected response from the EDA-server LLM: {str(d)[:200]}") from e


# ---------------------------------------------------------------------- conversations


class Conversations:
    """Chat history per project, bound to the connection each conversation started on."""

    def __init__(self, project_path: Path):
        self.dir = project_path / "llm" / "chats"

    def _p(self, cid: str) -> Path:
        if not cid.replace("-", "").isalnum():
            raise KeyError("bad conversation id")
        return self.dir / f"{cid}.json"

    def new(self, connection: dict, persist: bool = True) -> dict:
        """persist=False: written by the first save() (a chat whose first turn fails leaves no empty conversation)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        c = {"id": "chat-" + uuid.uuid4().hex[:8], "created": _now(), "connection": connection, "messages": [], "title": ""}
        if persist:
            self.save(c)
        return c

    def get(self, cid: str) -> dict:
        p = self._p(cid)
        if not p.is_file():
            raise KeyError(f"no conversation {cid}")
        return json.loads(p.read_text())

    def save(self, c: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._p(c["id"]).write_text(json.dumps(c, ensure_ascii=False, indent=1) + "\n")

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json"), reverse=True) if self.dir.is_dir() else []:
            c = json.loads(p.read_text())
            out.append({k: c.get(k) for k in ("id", "created", "connection", "title")} | {"turns": len(c["messages"]) // 2})
        return sorted(out, key=lambda c: c["created"], reverse=True)
