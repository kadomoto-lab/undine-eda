"""The agent guide (skills.py): an Agent Skill whose tools and links exist, served as MCP resources (also by mcp-stdio
without a server), shown and installed from the shell, and never read from outside its folder."""
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from undine.agents import agent_tools, mcp, skills  # noqa: E402
from undine.agents.mcp_local import OPEN_TOOL, Launcher  # noqa: E402
import test_changeset as tc  # noqa: E402


def test_the_guide_is_a_skill_whose_tools_and_links_exist():
    entry = skills.ROOT / "undine" / "SKILL.md"
    head = entry.read_text().split("---")[1]
    assert re.search(r"^name: undine$", head, re.M) and re.search(r"^description: .{40,}", head, re.M)
    tools = {t["name"] for t in agent_tools.specs()} | {OPEN_TOOL["name"]}
    for p in skills.files():
        text = p.read_text()
        for link in re.findall(r"\]\(([^)#:]+\.md)\)", text):            # relative links resolve inside the skill
            assert (p.parent / link).resolve().is_file(), f"{p.name}: broken link {link}"
        for name in re.findall(r"`([a-z]+(?:_[a-z]+)+)\(", text):         # `tool(...)` names a real tool
            assert name in tools, f"{p.name}: no tool {name}"
    names = [r["name"] for r in skills.resources()]
    assert names[0] == "undine/SKILL.md" and "undine/references/layout.md" in names


def test_read_by_uri_or_short_name_and_never_outside():
    layout = skills.read("undine://skills/undine/references/layout.md")
    assert layout.startswith("# Layout") and skills.read("layout") == layout and skills.read("references/layout") == layout
    assert skills.read("SKILL").startswith("---")
    for bad in ("../../mcp.py", "undine://skills/../../mcp.py", "nothing", "/etc/passwd"):
        try:
            skills.read(bad)
            raise AssertionError(bad)
        except KeyError:
            pass


def test_installs_into_an_agents_skills_folder():
    d = Path(tempfile.mkdtemp())
    (d / "undine").mkdir()
    (d / "undine" / "old.md").write_text("an older copy")
    skills.install(d)
    assert (d / "undine" / "SKILL.md").is_file() and (d / "undine" / "references" / "rtl.md").is_file()
    assert not (d / "undine" / "old.md").exists()
    shutil.rmtree(d)


def test_served_as_mcp_resources_by_the_server_and_by_mcp_stdio():
    root, srv, _ = tc.setup()
    srv.mcp_enabled = True
    auth = {"authorization": f"Bearer {mcp.ensure_token(srv.store.root)}"}
    sessions = mcp.Sessions()
    code, init, _ = mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, auth)
    assert "resources" in init["result"]["capabilities"] and "undine://skills/undine/SKILL.md" in init["result"]["instructions"]
    _, out, _ = mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 2, "method": "resources/list"}, auth)
    uris = [r["uri"] for r in out["result"]["resources"]]
    assert uris[0] == "undine://skills/undine/SKILL.md"
    _, out, _ = mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": uris[1]}}, auth)
    assert out["result"]["contents"][0]["text"].startswith("# ")
    _, out, _ = mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "undine://skills/x"}}, auth)
    assert out["error"]["code"] == -32002

    def down(*a, **k):
        raise OSError("refused")
    la = Launcher("http://127.0.0.1:9", start=False, opener=lambda u: None, http=down)   # no server: the guide still reads
    out = la.handle({"jsonrpc": "2.0", "id": 5, "method": "resources/list"})
    assert out["result"]["resources"][0]["name"] == "undine/SKILL.md"
    out = la.handle({"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": "undine://skills/undine/references/connect.md"}})
    assert "undine call" in out["result"]["contents"][0]["text"]
    shutil.rmtree(root)
