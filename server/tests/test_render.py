"""Pictures for agents (render.py; render_schematic, render_layout, plot_waveform): PNGs drawn by KLayout with the numbers
beside them, sent to MCP clients as image blocks, saved to files by the shell client, kept from text-only clients."""
import argparse
import base64
import io
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import klayout.db as kdb  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from undine.agents import agent_tools as T, mcp, render  # noqa: E402
from undine.server.api import create_app  # noqa: E402
from undine.agents import client  # noqa: E402
from undine.agents.client import Undine, add_parsers, main_call  # noqa: E402
import test_changeset as tc  # noqa: E402
from test_mcp_local import transport  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n"


def size_of(png: bytes) -> tuple[int, int]:
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def setup():
    root, srv, cs = tc.setup()
    srv.mcp_enabled = True
    db = srv.db("p", "l")
    met1 = db.layout.layer(68, 20)
    db.cell("top").shapes(met1).insert(kdb.DBox(0, 0, 2.0, 0.5))      # something to draw
    db.cell("top").shapes(db.layout.layer(67, 20)).insert(kdb.DBox(0, 1.0, 1.0, 1.5))
    db.lv.add_missing_layers()                                       # as an edit through the editor does
    return root, srv


def test_a_schematic_is_drawn_as_in_the_editor():
    root, srv = setup()
    d = srv.editor("p", "l", "top", "schematic").view()
    png, w, h = render.schematic_png(d, 640)
    assert png.startswith(PNG) and size_of(png) == (w, h) and max(w, h) == 640
    t = {"x": 0, "y": 0, "rot": 0, "flip": 0, "hc": True, "vc": False}
    assert render._text_box(t, 10, 4) == (-5, 0, 5, 4)                              # centred, as schdraw.ts does
    assert render._junctions([[0, 0, 10, 0, False], [10, 0, 20, 0, False], [10, 0, 10, 10, False]]) == [(10, 0)]
    shutil.rmtree(root)


def test_a_layout_is_drawn_in_a_box_and_on_chosen_layers():
    root, srv = setup()
    db = srv.db("p", "l")
    png, bb, w, h = db.render("top", size=500)
    assert png.startswith(PNG) and max(w, h) == 500 and bb.contains(kdb.DPoint(2.0, 1.5))
    png2, bb2, *_ = db.render("top", box=[0, 0, 1, 1], layers=["68/20"])
    assert (bb2.left, bb2.bottom, bb2.right, bb2.top) == (0, 0, 1, 1) and png2 != png
    try:
        db.render("top", box=[50, 50, 50, 50])
        raise AssertionError("an empty box")
    except KeyError:
        pass
    shutil.rmtree(root)


def test_waveforms_are_plotted_with_what_each_trace_spans():
    tran = {"plot": "Transient Analysis", "complex": False, "points": 3, "x": {"name": "time", "unit": "time", "values": [0, 1e-9, 2e-9]},
            "vectors": [{"name": "v(out)", "type": "voltage"}], "traces": {"v(out)": {"y": [0.0, 1.8, 0.9]}}}
    png, legend = render.waveform_png(tran, (800, 400))
    assert png.startswith(PNG) and size_of(png) == (800, 400)
    assert legend == [{"signal": "v(out)", "color": "#72bfc6", "min": 0.0, "max": 1.8}]
    ac = {"plot": "AC Analysis", "complex": True, "points": 3, "x": {"name": "frequency", "unit": "frequency", "values": [1, 1e3, 1e6]},
          "vectors": [{"name": "v(out)", "type": "voltage"}], "traces": {"v(out)": {"mag_db": [40, 37, -20], "phase_deg": [0, -45, -170]}}}
    png, legend = render.waveform_png(ac)
    assert png.startswith(PNG) and legend[0]["max"] == 40
    assert [round(v) for v in render._ticks(0, 1.8)] == [0, 0, 1, 1, 2] or render._ticks(0, 1.8)[-1] <= 1.8
    assert render._si(1.5e-9) == "1.5n" and render._si(2e3) == "2k" and render._si(0) == "0"


def test_pictures_reach_mcp_clients_as_images_and_files_from_the_shell(tmp_path):
    root, srv = setup()
    auth = {"authorization": f"Bearer {mcp.ensure_token(srv.store.root)}"}
    sessions = mcp.Sessions()
    _, _, hdr = mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, auth)
    h = {**auth, "mcp-session-id": hdr["mcp-session-id"]}
    _, out, _ = mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "render_layout", "arguments": {"project": "p", "lib": "l", "cell": "top", "layers": ["68/20"], "size": 300}}}, h)
    content = out["result"]["content"]
    info = json.loads(content[0]["text"])
    assert info["image_attached"] and info["box_um"][2] >= 2.0 and info["um_per_px"] > 0
    assert content[1]["type"] == "image" and base64.b64decode(content[1]["data"]).startswith(PNG)
    _, out, _ = mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "plot_waveform", "arguments": {"project": "p", "lib": "l", "cell": "top"}}}, h)
    assert out["result"]["isError"] and "run_simulation first" in out["result"]["content"][0]["text"]
    assert len(out["result"]["content"]) == 1                                        # an error carries no picture
    audit = (root / "p" / "llm" / "audit.jsonl").read_text()
    assert '"images":1' in audit and "iVBORw0KGgo" not in audit                     # counted, not copied

    mcp_http = transport(TestClient(create_app(srv, None)))
    u = Undine("p", "http://undine.test", http=mcp_http)
    res = u.render_schematic(lib="l", cell="top", size=400)
    assert res["images"][0].startswith(PNG) and res["instances"] == 1
    os.environ["UNDINE_CLI_SESSIONS"] = str(tmp_path / "s.json")
    ap = argparse.ArgumentParser()
    add_parsers(ap.add_subparsers(dest="cmd"))
    args = ap.parse_args(["call", "render_schematic", "lib=l", "cell=top", "--project", "p", "--url", "http://undine.test",
                          "--out", str(tmp_path / "top.png")])
    o, e = io.StringIO(), io.StringIO()
    assert main_call(args, http=mcp_http, out=o, err=e) == 0, e.getvalue()
    assert json.loads(o.getvalue())["images"] == [str(tmp_path / "top.png")] and (tmp_path / "top.png").read_bytes().startswith(PNG)
    # no --out: a new file beside the sessions file (an agent's own folder), never the shared temporary directory;
    # --out-dir, or --out naming a directory, puts it there
    for extra, where in (([], tmp_path), (["--out-dir", str(tmp_path / "pics")], tmp_path / "pics"),
                         (["--out", str(tmp_path / "pics2") + "/"], tmp_path / "pics2")):
        args = ap.parse_args(["call", "render_schematic", "lib=l", "cell=top", "--project", "p", "--url",
                              "http://undine.test", *extra])
        o, e = io.StringIO(), io.StringIO()
        assert main_call(args, http=mcp_http, out=o, err=e) == 0, e.getvalue()
        got = Path(json.loads(o.getvalue())["images"][0])
        assert got.parent == where.resolve() and got.name.startswith("undine-render_schematic-")
        assert got.read_bytes().startswith(PNG)
    del os.environ["UNDINE_CLI_SESSIONS"]
    assert client.picture_dir() == Path(".") and client.picture_dir(str(tmp_path)) == tmp_path
    shutil.rmtree(root)


def test_text_only_clients_and_steps_do_not_get_picture_tools():
    names = {t["name"] for t in T.specs(pictures=False)}
    assert not names & {"render_schematic", "render_layout", "plot_waveform"} and "get_netlist" in names
    assert {"render_schematic", "render_layout", "plot_waveform"} <= {t["name"] for t in mcp.tool_list()}
    root, srv = setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:test", owner="mcp:s", owner_kind="mcp")
    T.call(ctx, "open_changeset", {"title": "t", "reason": "r"})
    text, err = T.call(ctx, "batch", {"ops": [{"tool": "render_schematic", "args": {"lib": "l", "cell": "top"}}]})
    assert err and "cannot be part of batch" in text
    shutil.rmtree(root)
