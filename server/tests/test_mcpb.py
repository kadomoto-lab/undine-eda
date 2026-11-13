"""The Claude Desktop extension bundle: a valid manifest (0.3) that lists the tools, an executable launcher, the icon."""
import json
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.agents import agent_tools as T  # noqa: E402
from undine.agents import mcpb  # noqa: E402


def test_bundle():
    d = Path(tempfile.mkdtemp())
    out = mcpb.build(d / "undine.agents.mcpb")
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == ["icon.png", "manifest.json", "server/launch.sh"]
        m = json.loads(z.read("manifest.json"))
        assert stat.S_IMODE(z.getinfo("server/launch.sh").external_attr >> 16) == 0o755
        launch = z.read("server/launch.sh").decode()
    assert m["manifest_version"] == "0.3" and m["server"]["type"] == "binary" and m["version"] == mcpb.version()
    assert {t["name"] for t in m["tools"]} == {t["name"] for t in T.specs()} | {"open_in_undine"}
    cfg = m["server"]["mcp_config"]
    assert cfg["args"] == ["${__dirname}/server/launch.sh"] and cfg["env"]["UNDINE_DIR"] == "${user_config.undine_dir}"
    win = cfg["platform_overrides"]["win32"]
    assert win["command"] == "wsl.exe" and "mcp-stdio" in win["args"][-1] and "${user_config.undine_dir}" in win["args"][-1]
    assert m["user_config"]["undine_dir"]["required"] and "mcp-stdio" in launch
    # Undine on another machine: an optional SSH host, passed on every platform
    assert not m["user_config"]["ssh_host"]["required"] and cfg["env"]["UNDINE_SSH"] == "${user_config.ssh_host}"
    assert '--ssh "$UNDINE_SSH"' in launch and "UNDINE_SSH='${user_config.ssh_host}'" in win["args"][-1]
    shutil.rmtree(d)
