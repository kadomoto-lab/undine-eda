"""LLM connections registered from the UI: key checked by listing models, stored 0600 outside projects, never
returned, used for calls (OpenAI-compatible and Anthropic), policy by where the endpoint is, removal falls back."""
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402

from undine.agents import llm  # noqa: E402
from undine.server.api import create_app  # noqa: E402
import test_changeset as tc  # noqa: E402

KEY = "sk-test-1234567890abcd"
SEEN: list[dict] = []


class Fake(BaseHTTPRequestHandler):
    def _out(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _authed(self):
        return self.headers.get("authorization") == f"Bearer {KEY}" or self.headers.get("x-api-key") == KEY

    def do_GET(self):
        if not self._authed():
            return self._out(401, {"error": "bad key"})
        self._out(200, {"data": [{"id": "model-a"}, {"id": "model-b"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SEEN.append({"path": self.path, "auth": self.headers.get("authorization") or self.headers.get("x-api-key"), "model": body.get("model")})
        if not self._authed():
            return self._out(401, {"error": "bad key"})
        if self.path.endswith("/v1/messages"):            # Anthropic-shaped
            return self._out(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                                   "content": [{"type": "text", "text": "hello from anthropic"}], "stop_reason": "end_turn",
                                   "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
        self._out(200, {"choices": [{"message": {"content": "hello from openai"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    def log_message(self, *a):
        pass


def setup(policy=None):
    home = Path(tempfile.mkdtemp())
    os.environ["UNDINE_HOME"] = str(home)
    root, srv, cs = tc.setup()
    srv.gateway = llm.Gateway(root, policy)
    httpd = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return home, root, srv, TestClient(create_app(srv, None)), f"http://127.0.0.1:{httpd.server_port}"


def ask(c):
    return c.post("/api/p/p/chat?wait=true", json={"message": "hi", "mode": "ask",
                  "context": {"lib": "l", "cell": "top", "view": "schematic", "selection": [], "overview": False}}).json()


def test_register_use_and_remove_an_openai_compatible_connection():
    home, root, srv, c, url = setup()
    bad = c.post("/api/llm/connections", json={"label": "Mine", "provider": "openai", "api_key": "wrong", "base_url": url + "/v1"})
    assert bad.status_code == 502 and "not saved" in bad.json()["detail"] and not (home / "connections.json").exists()
    r = c.post("/api/llm/connections", json={"label": "Mine", "provider": "openai", "api_key": KEY, "base_url": url + "/v1"}).json()
    cid = r["added"]
    assert r["available_models"] == ["model-a", "model-b"]
    conn = next(x for x in r["connections"] if x["id"] == cid)
    assert conn["key_hint"] == "…abcd" and conn["where"] == "server" and "key" not in conn
    assert KEY not in json.dumps(c.get("/api/llm").json())                                    # never sent back
    assert stat.S_IMODE((home / "connections.json").stat().st_mode) == 0o600
    s = c.post("/api/llm/connection", json={"connection": cid, "model": "model-b"}).json()
    assert s["active"]["connection"] == cid and s["label"] == "Mine · model-b" and s["ready"]
    out = ask(c)
    assert out["reply"] == "hello from openai", out
    assert SEEN[-1]["auth"] == f"Bearer {KEY}" and SEEN[-1]["model"] == "model-b"
    assert KEY not in (root / "p" / "llm" / "audit.jsonl").read_text()                          # not in the audit log
    s = c.delete(f"/api/llm/connections/{cid}").json()
    assert not s["connections"] and s["active"].get("connection") is None
    shutil.rmtree(root), shutil.rmtree(home)


def test_an_anthropic_connection_uses_its_own_key_and_endpoint():
    home, root, srv, c, url = setup()
    r = c.post("/api/llm/connections", json={"label": "Team key", "provider": "anthropic", "api_key": KEY, "base_url": url}).json()
    c.post("/api/llm/connection", json={"connection": r["added"], "model": "model-a"})
    out = ask(c)
    assert out["reply"] == "hello from anthropic", out
    assert SEEN[-1]["path"].endswith("/v1/messages") and KEY in SEEN[-1]["auth"]   # x-api-key or Bearer, by SDK version
    shutil.rmtree(root), shutil.rmtree(home)


def test_policy_applies_by_where_the_endpoint_is():
    home, root, srv, c, url = setup(policy="cloud")
    r = c.post("/api/llm/connections", json={"label": "Local", "provider": "openai", "api_key": KEY, "base_url": url + "/v1"}).json()
    s = c.post("/api/llm/connection", json={"connection": r["added"], "model": "model-a"})
    assert s.status_code == 403 and "does not allow server" in s.json()["detail"]
    assert llm.Connections(home).add("Cloud", "openai", "k", "https://api.example.com/v1", ["x"])["where"] == "cloud"
    try:
        llm.Connections(home).add("NoKey", "anthropic", "", None, ["x"])
        raise AssertionError("a cloud endpoint needs a key")
    except ValueError:
        pass
    shutil.rmtree(root), shutil.rmtree(home)
