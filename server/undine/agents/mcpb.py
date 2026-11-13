"""The Claude Desktop extension (MCPB bundle): `python -m undine mcpb [--out FILE]`.

The bundle holds no server code. It starts the local MCP entry (`mcp_local.py`, `undine mcp-stdio`) from the user's
Undine checkout, which starts or reuses the Undine server and opens the review in the browser:

* macOS / Linux: `server/launch.sh` in the bundle runs the checkout's pixi Python.
* Windows: Undine runs in WSL; `wsl.exe` runs the same command there (the browser opens on the Windows side).

The settings Claude Desktop asks for are the checkout's location (a WSL path on Windows) and, optionally, the server and UI
URLs and an SSH host when the server runs on another machine. Manifest format: https://github.com/modelcontextprotocol/mcpb (manifest_version 0.3).
"""
from __future__ import annotations

import json
import tomllib
import zipfile
from pathlib import Path

from undine.agents import agent_tools as T
from undine.agents.mcp_local import OPEN_TOOL

PKG = Path(__file__).resolve().parents[1]          # the undine package

LAUNCH_SH = r"""#!/bin/sh
# Started by Claude Desktop (Undine extension): runs Undine's local MCP entry from the checkout in $UNDINE_DIR.
dir="$UNDINE_DIR"
case "$dir" in "~"|"~/"*) dir="$HOME${dir#"~"}" ;; esac
py="$dir/.pixi/envs/default/bin/python"
if [ ! -x "$py" ]; then
  echo "undine-mcp: no Undine checkout with its pixi environment at $dir (run 'pixi install' there, or fix the path in the extension's settings)" >&2
  exit 1
fi
cd "$dir/server" || exit 1
exec "$py" -m undine mcp-stdio ${UNDINE_URL:+--url "$UNDINE_URL"} ${UNDINE_UI_URL:+--ui-url "$UNDINE_UI_URL"} ${UNDINE_SSH:+--ssh "$UNDINE_SSH"}
"""

# Windows: the same in WSL. ~ and $HOME expand there; a checkout path with spaces is not supported.
WSL_COMMAND = ("cd ${user_config.undine_dir}/server && UNDINE_URL='${user_config.server_url}' "
               "UNDINE_UI_URL='${user_config.ui_url}' UNDINE_SSH='${user_config.ssh_host}' exec ../.pixi/envs/default/bin/python -m undine mcp-stdio")


def version() -> str:
    return tomllib.loads((PKG.parent / "pyproject.toml").read_text())["project"]["version"]


def manifest() -> dict:
    tools = [{"name": t["name"], "description": t["description"].split(". ")[0].rstrip(".") + "."} for t in T.specs()]
    tools.append({"name": OPEN_TOOL["name"], "description": OPEN_TOOL["description"]})
    return {
        "manifest_version": "0.3",
        "name": "undine",
        "display_name": "Undine EDA",
        "version": version(),
        "description": "Design analog ICs in Undine from Claude: read and change schematics and layouts as reviewable proposals.",
        "long_description": (
            "Connects Claude to Undine, an AI-native, web-based EDA environment for custom IC design, running on your computer. "
            "Claude can read the design (cells, connectivity, DRC / LVS results, history) and propose changes: every edit "
            "goes into a changeset, a working copy you watch grow in Undine and review, check and apply yourself; Claude "
            "cannot apply it. Undine is started if it is not running, and the review opens in your browser when work starts."),
        "author": {"name": "Undine contributors"},
        "repository": {"type": "git", "url": "https://github.com/kadomoto-lab/undine-eda"},
        "license": "Apache-2.0",
        "icon": "icon.png",
        "server": {
            "type": "binary",
            "entry_point": "server/launch.sh",
            "mcp_config": {
                "command": "/bin/sh",
                "args": ["${__dirname}/server/launch.sh"],
                "env": {"UNDINE_DIR": "${user_config.undine_dir}", "UNDINE_URL": "${user_config.server_url}",
                        "UNDINE_UI_URL": "${user_config.ui_url}", "UNDINE_SSH": "${user_config.ssh_host}"},
                "platform_overrides": {"win32": {"command": "wsl.exe", "args": ["-e", "bash", "-lc", WSL_COMMAND], "env": {}}},
            },
        },
        "tools": tools,
        "user_config": {
            "undine_dir": {
                "type": "string", "title": "Undine checkout", "required": True, "default": "~/undine-eda",
                "description": "Where undine-eda is checked out, with its pixi environment installed. On Windows: the path "
                               "inside WSL (e.g. ~/undine-eda)."},
            "server_url": {
                "type": "string", "title": "Undine server URL", "required": False, "default": "",
                "description": "Leave empty for http://127.0.0.1:8700. Undine is started there if it is not running "
                               "(with an SSH host: the server as seen on that host, never started from here)."},
            "ssh_host": {
                "type": "string", "title": "SSH host (Undine on another machine)", "required": False, "default": "",
                "description": "Leave empty when Undine runs on this computer. Otherwise user@host or an ~/.ssh/config alias "
                               "(on Windows: as ssh in WSL knows it): a port is forwarded there and the token read over SSH. "
                               "Needs a key or an ssh-agent, no password prompt."},
            "ui_url": {
                "type": "string", "title": "Undine UI URL", "required": False, "default": "",
                "description": "Leave empty to open the UI the server serves; set it when you use the Vite dev server "
                               "(e.g. http://localhost:5173)."},
        },
        "compatibility": {"platforms": ["darwin", "win32", "linux"]},
    }


def build(out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data, mode in (("manifest.json", json.dumps(manifest(), indent=2) + "\n", 0o644),
                                  ("icon.png", (PKG / "data" / "mcpb" / "icon.png").read_bytes(), 0o644),
                                  ("server/launch.sh", LAUNCH_SH, 0o755)):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return out
