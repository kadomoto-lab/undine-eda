"""LLM gateway rules (policy, exclusivity, no fallback, audit, conversation binding) and the context builder."""
import json
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.agents import llm, llm_context  # noqa: E402
import test_changeset as tc  # noqa: E402

SEEN: list[dict] = []


class Fake(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SEEN.append(body)
        out = json.dumps({"choices": [{"message": {"content": "ok: " + str(body["messages"][-1]["content"])[-40:]}, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def fake_server():
    httpd = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}/v1"


def test_policy_exclusive_and_no_fallback():
    root = Path(tempfile.mkdtemp())
    httpd, url = fake_server()
    gw = llm.Gateway(root, "server")
    assert gw.allowed() == ["server"]
    try:
        gw.switch("cloud", None, None)
        raise AssertionError("cloud allowed under policy 'server'")
    except llm.LlmPolicyError:
        pass
    gw.switch("server", "m1", url)
    r = gw.complete(root, "chat", "sys", [{"role": "user", "content": "hello"}], "c1")
    assert r["text"].startswith("ok:") and r["connection"] == {"kind": "server", "model": "m1"}
    assert SEEN[-1]["messages"][0] == {"role": "system", "content": "sys"}
    # the endpoint dies: the error is returned, nothing is sent anywhere else
    httpd.shutdown(); httpd.server_close()
    try:
        gw.complete(root, "chat", "sys", [{"role": "user", "content": "again"}], "c1")
        raise AssertionError("expected a connection error")
    except llm.LlmError as e:
        assert "cannot reach" in str(e)
    audit = [json.loads(l) for l in (root / "llm" / "audit.jsonl").read_text().splitlines()]
    assert [a["ok"] for a in audit] == [True, False] and audit[0]["payload"]["messages"][0]["content"] == "hello"
    assert audit[0]["connection"]["kind"] == "server" and "api.anthropic.com" not in json.dumps(audit)
    shutil.rmtree(root)


def test_cloud_only_when_active_and_allowed(monkeypatch_calls=[]):
    import anthropic

    root = Path(tempfile.mkdtemp())
    calls = []

    class FakeClient:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kw):
                calls.append(kw)
                from types import SimpleNamespace as N
                return N(content=[N(type="text", text="cloud answer")], stop_reason="end_turn", usage=N(input_tokens=5, output_tokens=2))

    orig = anthropic.Anthropic
    anthropic.Anthropic = lambda *a, **k: FakeClient
    try:
        gw = llm.Gateway(root, "both")
        assert gw.active()["kind"] == "cloud" and gw.active()["model"] == "claude-haiku-5-5"
        assert gw.complete(root, "chat", "s", [{"role": "user", "content": "q"}])["text"] == "cloud answer"
        assert calls[0]["model"] == "claude-haiku-5-5" and "fallbacks" not in calls[0] and "betas" not in calls[0]
        # a conversation bound to another connection is refused, not silently moved
        try:
            gw.complete(root, "chat", "s", [{"role": "user", "content": "q"}], connection={"kind": "server", "model": "m1"})
            raise AssertionError("conversation moved silently")
        except llm.LlmError as e:
            assert "belongs to" in str(e)
        # the policy forbids cloud: no client is created at all
        gw.switch("cloud", None, None)                                   # stored choice: cloud
        gw2 = llm.Gateway(root, "server")                                # ... but this server forbids it
        n = len(calls)
        try:
            gw2.complete(root, "chat", "s", [{"role": "user", "content": "q"}])
            raise AssertionError("cloud call under policy server")
        except llm.LlmPolicyError:
            pass
        assert len(calls) == n
        try:
            llm.Gateway(root, "none").complete(root, "chat", "s", [])
            raise AssertionError
        except llm.LlmPolicyError:
            pass
    finally:
        anthropic.Anthropic = orig
    shutil.rmtree(root)


def test_context_resolves_ids_and_pins_revision():
    root, srv, cs = tc.setup()
    ed = srv.editor("p", "l", "top", "schematic")
    with ed.recording("set_props", {}, "agent:t", "less current"):
        ed.set_props("i", "R1", {"value": "2k"})
    text, summary = llm_context.build(srv, "p", {"lib": "l", "cell": "top", "view": "schematic", "revision": "x",
                                                  "selection": [{"k": "i", "id": "R1"}, {"k": "i", "id": "NOPE"}]})
    assert "instance R1" in text and "value=2k" in text and "less current" in text
    assert "NOPE" in text and "not found" in text and summary["objects"] == ["instance R1"]
    r1 = llm_context.revision(srv, "p", "l", "top")
    ed.set_props("i", "R1", {"value": "3k"})
    assert llm_context.revision(srv, "p", "l", "top") != r1           # the pin moves with the design
    shutil.rmtree(root)



def test_images_are_converted_audited_by_hash_and_validated():
    import base64
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from undine.server import api as apimod

    root = Path(tempfile.mkdtemp())
    httpd, url = fake_server()
    gw = llm.Gateway(root, "server")
    gw.switch("server", "vision-m", url)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfakepixels").decode()
    msgs = [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": png}},
                                          {"type": "text", "text": "what is this?"}]}]
    gw.complete(root, "chat", "sys", msgs, "c1")
    sent = SEEN[-1]["messages"][-1]["content"]
    assert sent[0] == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}} and sent[1]["text"] == "what is this?"
    audit = (root / "llm" / "audit.jsonl").read_text()
    assert png not in audit                                                     # the log holds a reference, not the pixels
    rec = json.loads(audit.splitlines()[-1])
    ref = rec["payload"]["messages"][0]["content"][0]
    assert ref["type"] == "image" and (root / "llm" / "images" / f"{ref['sha1']}.png").read_bytes().startswith(b"\x89PNG")
    # validation
    from types import SimpleNamespace as N
    good = N(media_type="image/png", data=png, label="x")
    assert apimod._check_images([good]) == [good]
    for bad in (N(media_type="image/gif", data=png, label=""), N(media_type="image/png", data="@@@", label="")):
        try:
            apimod._check_images([bad])
            raise AssertionError("accepted")
        except llm.LlmError:
            pass
    try:
        apimod._check_images([good] * 3)
        raise AssertionError("accepted 3")
    except llm.LlmError:
        pass
    httpd.shutdown()
    shutil.rmtree(root)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
