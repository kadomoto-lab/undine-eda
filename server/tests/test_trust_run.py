"""The trust gate (store/trust.py's policy) at the places a dependency's text would run: an ngspice job (start_sim),
an LVS run when it launches (paths only), Xschem's own netlister (Tcl only) and Verilator (rtl.py). A dependency is
untrusted until a person trusts its key in project.json; a project's own cells run as before; a netlist only shown
warns instead of refusing."""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402
from undine import tools  # noqa: E402
from undine.design import rtl  # noqa: E402
from undine.pdks.pdk import Pdk  # noqa: E402
from undine.schematics import schematic, xschem  # noqa: E402
from undine.server import api as apimod  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store import cas, deps, trust  # noqa: E402
from undine.store.project import Project  # noqa: E402
from test_netlist_sources import CORNER_SYM, R_SYM, _cell, _code, _devices, _r, _sub  # noqa: E402

COMMIT = "229914698b1f" + "0" * 28
CONTROL = ".control\nshell echo hi\n.endc"
MANIFEST = '[package]\nname = "analog_ip"\nversion = "1.2.0"\n'
# a resistor symbol whose template gives every instance a value holding a .control block
BAD_R_SYM = R_SYM.replace('template="name=R1 value=1k"',
                          'template="name=R1 value=\\\\\\"1k\n.control\nshell x\n.endc\\\\\\""')


def _tcl_sym(made: Path) -> str:
    """A symbol whose format Xschem evaluates as Tcl: it would make `made` (in the test's tmp_path, never /tmp)."""
    return ('v {xschem version=3.4.5 file_version=1.2}\nK {type=netlist_commands\n'
            f'format="tcleval([file mkdir {made.as_posix()}])"\ntemplate="name=t1"}}\n')


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def _server(tmp_path: Path, pdk: Pdk | None = None) -> Server:
    _devices()
    root = tmp_path / "projects"
    srv = Server(root, {pdk.id: pdk} if pdk else {}, [root])
    srv.store.create("p", pdk.id if pdk else None).create_library("l")
    return srv


def _install(srv: Server, local: str = "ip", manifest: str | None = None) -> Path:
    """A dependency as lib sync installs it, made by hand: named in project.json, its files in .undine/deps/<local>
    with the install marker."""
    prj = srv.project("p")
    with prj.editing():
        prj.meta["libraries"][local] = {"git": "https://example.invalid/ip.git", "rev": "v1"}
        prj.save()
    d = prj.path / deps.DEPS / local
    d.mkdir(parents=True)
    (d / cas.MARKER).write_text(json.dumps({"git": "https://example.invalid/ip.git", "rev": "v1", "commit": COMMIT,
                                            "name": local}))
    if manifest:
        (d / "undine-package.toml").write_text(manifest)
    return d


def _tb(srv: Server, comps: list[str]) -> None:
    _cell(srv.project("p").library("l").path, "tb", comps, ports=False)


def _runs(srv: Server) -> list[Path]:
    d = srv.project("p").path / "runs"
    return sorted(d.iterdir()) if d.is_dir() else []


def _refused(fn) -> trust.UntrustedContent:
    with pytest.raises(trust.UntrustedContent) as e:
        fn()
    return e.value


def _done(job, limit: float = 60) -> None:
    t = time.time() + limit
    while job.status()["state"] in ("queued", "running") and time.time() < t:
        time.sleep(0.1)


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


# ------------------------------------------------------------------ ngspice


def test_a_dependency_cell_with_control_or_an_absolute_include_is_refused_until_trusted(tmp_path):
    if not shutil.which("git") or not tools.find("ngspice"):
        pytest.skip("needs git and ngspice")
    srv = _server(tmp_path)
    repo = tmp_path / "src" / "ip"
    _cell(repo / "lib", "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s1", ".control\necho trusted\n.endc")])
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v1")
    _git(repo, "tag", "v1")
    prj = srv.project("p")
    commit = deps.add(prj, "ip", str(repo), "v1", "lib")["commit"]
    _tb(srv, _sub("ip/inv.sym", "x1"))

    e = _refused(lambda: srv.start_sim("p", "l", "tb", {"type": "op"}))
    key = f"ip@{commit[:12]}"
    assert (e.name, e.package, e.key, e.cell) == ("ip", None, key, "ip/inv")
    assert [(f, x.kind, x.line) for f, x in e.findings] == [("ip/inv.sch", "control", 1)]
    assert str(e).startswith(f"ip/inv (dependency git {commit[:12]}, not trusted): code block s1 line 1: .control")
    assert str(e).endswith(f"a person can trust {key} in this project (undine lib trust p ip, or Trust... in "
                           "Undine); an agent cannot")
    assert _runs(srv) == []                                  # refused before any job directory

    assert trust.set_trusted(prj, key, True) and not trust.set_trusted(prj, key, True)
    assert json.loads((prj.path / "project.json").read_text())["trusted"] == [key]
    job = srv.start_sim("p", "l", "tb", {"type": "op"})     # trusted: the run starts
    _done(job)
    assert len(_runs(srv)) == 1

    # an absolute .include in the same dependency is other content, refused in the same way while not trusted
    assert trust.set_trusted(prj, key, False)
    d = deps.dep_dir(prj.path, "ip")
    for f in ("inv.sch", "inv.sym"):                         # links into the store: replaced, never written
        (d / f).unlink()
    _cell(d, "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s1", ".include /etc/hostname")])
    e = _refused(lambda: srv.start_sim("p", "l", "tb", {"type": "op"}))
    assert [(x.kind, x.path) for _, x in e.findings] == [("path", "/etc/hostname")]


def test_a_project_symbols_value_set_in_a_dependency_cell_is_refused(tmp_path):
    """Home attribution: devices/res.sym is no dependency's, but the value its line is formed from is written in the
    dependency's schematic."""
    srv = _server(tmp_path)
    d = _install(srv)
    _cell(d, "inv", _r("devices/res.sym", "R1", "1k\n.control\nshell rm -rf x\n.endc"))
    _tb(srv, _sub("ip/inv.sym", "x1"))
    e = _refused(lambda: srv.start_sim("p", "l", "tb", {"type": "op"}))
    assert [(f, x.kind, x.line) for f, x in e.findings] == [("ip/inv.sch", "control", 2)]
    assert "ip/inv (dependency git 229914698b1f, not trusted): instance R1 line 2: .control" in str(e)


def test_a_dependency_symbols_template_default_is_refused_in_its_own_cell_and_in_a_project_cell(tmp_path):
    srv = _server(tmp_path)
    d = _install(srv, manifest=MANIFEST)
    (d / "bad.sym").write_text(BAD_R_SYM)
    _cell(d, "inv", _r("bad.sym", "R1"))                      # its own symbol, placed without a value
    _tb(srv, _sub("ip/inv.sym", "x1"))
    e = _refused(lambda: srv.start_sim("p", "l", "tb", {"type": "op"}))
    assert (e.package, e.key, e.cell) == ("analog_ip", "analog_ip@1.2.0", "ip/inv")
    assert [f for f, _ in e.findings] == ["ip/inv.sch"]
    assert "(dependency analog_ip 1.2.0, not trusted)" in str(e) and "trust analog_ip@1.2.0 in this project" in str(e)

    # the same symbol placed in a project cell: the line is formed by the dependency's symbol
    _tb(srv, _r("ip/bad.sym", "R9"))
    e = _refused(lambda: srv.start_sim("p", "l", "tb", {"type": "op"}))
    assert [f for f, _ in e.findings] == ["ip/bad.sym"] and e.cell == "ip/bad"
    assert "ip/bad (dependency analog_ip 1.2.0, not trusted): its symbol, as instance R9 in tb, line 2" in str(e)


def test_an_absolute_include_of_a_file_in_the_dependencys_own_install_is_refused(tmp_path):
    srv = _server(tmp_path)
    d = _install(srv)
    (d / "models.spice").write_text("* models\n")
    _cell(d, "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s1", f".include {d / 'models.spice'}")])
    _tb(srv, _sub("ip/inv.sym", "x1"))
    e = _refused(lambda: srv.start_sim("p", "l", "tb", {"type": "op"}))
    assert [(x.kind, x.path) for _, x in e.findings] == [("path", str(d / "models.spice"))]


def test_star_hash_lines_and_control_in_a_dependency_subcircuit_are_refused(tmp_path):
    srv = _server(tmp_path)
    d = _install(srv)
    _cell(d, "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s1", "*#shell echo hi"),
                     _code("s2", CONTROL, y=400)])
    _tb(srv, _sub("ip/inv.sym", "x1"))
    e = _refused(lambda: srv.start_sim("p", "l", "tb", {"type": "op"}))
    assert sorted(x.kind for _, x in e.findings) == ["control", "control_line"]
    assert "code block s1 line 1: *#shell echo hi" in str(e) and "code block s2 line 1: .control" in str(e)


def test_includes_under_the_pdk_root_and_the_generated_corner_line_pass(tmp_path):
    """A path that resolves under the PDK's root passes, written with $PDK_ROOT or not; Undine's corner .lib line is
    its own; a path that climbs out of the PDK through $PDK_ROOT does not."""
    _devices()
    pdk = tmp_path / "pdks" / "fakeA"
    (pdk / "libs.tech").mkdir(parents=True)
    env = {"PDK_ROOT": str(pdk.parent), "UNDINE_MODEL_LIB": str(pdk / "libs.tech" / "models.lib"),
           "UNDINE_CORNERS": "tt ss"}
    prj = tmp_path / "prj"
    d = prj / deps.DEPS / "ip"
    d.mkdir(parents=True)
    (d / "corner.sym").write_text(CORNER_SYM)
    lib = prj / "l"

    def check(code: str):
        _cell(d, "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s1", code),
                         "C {corner.sym} 0 600 0 0 {name=c1 corner=tt}"])
        _cell(lib, "tb", _sub("ip/inv.sym", "x1"), ports=False)
        res = xschem.SymbolResolver([prj / deps.DEPS, *_devices()])
        r = schematic.spice(lib / "tb.sch", res, env, top_subckt=False)
        assert f".lib {env['UNDINE_MODEL_LIB']} tt" in r.text
        trust.check_netlist(r, untrusted=trust.untrusted_dirs(prj), pdk_root=pdk, env=env, rules="sim")

    check(".include $PDK_ROOT/fakeA/libs.tech/models.spice")
    check(f".lib {pdk}/libs.tech/models.lib ss")
    with pytest.raises(trust.UntrustedContent) as e:
        check(".include $PDK_ROOT/../../x")
    assert [x.path for _, x in e.value.findings] == [f"{pdk.parent}/../../x"]      # as the netlist writes it
    with pytest.raises(trust.UntrustedContent):
        check(".include $PDK_ROOT/fakeB/models.spice")         # another PDK's root is not this one


def test_a_project_cells_own_control_runs_and_display_netlists_only_warn(tmp_path):
    if not tools.find("ngspice"):
        pytest.skip("ngspice not installed")
    srv = _server(tmp_path)
    d = _install(srv)
    _cell(d, "inv", _r("devices/res.sym", "R1", "1k"))         # nothing to refuse
    _tb(srv, [*_sub("ip/inv.sym", "x1"), _code("s1", ".control\necho own\n.endc")])
    job = srv.start_sim("p", "l", "tb", {"type": "op"})       # the project's own .control is the project's
    _done(job)

    (d / "inv.sch").unlink()
    _cell(d, "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s9", CONTROL)])
    _refused(lambda: srv.start_sim("p", "l", "tb", {"type": "op"}))
    assert srv.netlist_sha("p", "l", "tb", "tb", "sim")       # hashing a netlist runs nothing
    c = TestClient(create_app(srv, None))
    r = c.get("/api/p/p/l/l/c/tb/netlist")
    assert r.status_code == 200, r.text
    assert any(w.startswith("not trusted: would be refused in a simulation: ip/inv") for w in r.json()["warnings"])


def test_http_sim_refusal_is_403_untrusted_with_findings(tmp_path):
    srv = _server(tmp_path)
    d = _install(srv)
    _cell(d, "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s1", CONTROL)])
    _tb(srv, _sub("ip/inv.sym", "x1"))
    want = str(_refused(lambda: srv.start_sim("p", "l", "tb", {"type": "op"})))
    c = TestClient(create_app(srv, None))
    r = c.post("/api/p/p/sim", json={"lib": "l", "cell": "tb", "analysis": {"type": "op"}})
    assert r.status_code == 403
    body = r.json()
    assert body["code"] == "untrusted" and body["detail"] == want          # the text as before, the rest added
    assert (body["name"], body["package"], body["key"], body["cell"]) == ("ip", None, "ip@229914698b1f", "ip/inv")
    assert body["findings"] == [{"file": "ip/inv.sch", "kind": "control", "line": 1, "text": ".control"}]
    assert _runs(srv) == []


# ------------------------------------------------------------------ LVS


def _fake_sky130(tmp_path: Path) -> Pdk:
    root = tmp_path / "pdks" / "fakeA"
    (root / "libs.tech" / "klayout" / "lvs").mkdir(parents=True)
    (root / "libs.tech" / "klayout" / "lvs" / "sky130.lvs").write_text("# a deck\n")
    return Pdk("fakeA", "sky130", "", root, klayout_tech=root / "libs.tech" / "klayout")


def test_lvs_is_refused_only_at_launch_and_only_for_paths(tmp_path, monkeypatch):
    srv = _server(tmp_path, _fake_sky130(tmp_path))
    d = _install(srv)
    _cell(d, "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s1", ".include /etc/hostname")])
    _cell(srv.project("p").library("l").path, "top", _sub("ip/inv.sym", "x1"))
    db = srv.db("p", "l")
    with db.lock:
        db.layout.create_cell("top")
    started = []
    monkeypatch.setattr(apimod.verify, "start_lvs", lambda *a, **k: started.append(a) or "job")

    checks = {c["name"]: c for c in srv.signoff("p", "l", "top")}   # status is shown, nothing refused
    assert "error" not in checks["KLayout LVS"], checks
    run = srv.prepare_lvs("p", "l", "top")
    e = _refused(run.launch)
    assert [(x.kind, x.path) for _, x in e.findings] == [("path", "/etc/hostname")] and not started

    # .control runs nowhere in KLayout or netgen: not refused there
    (d / "inv.sch").unlink()
    _cell(d, "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s1", CONTROL)])
    assert srv.prepare_lvs("p", "l", "top").launch() == "job" and len(started) == 1

    # trust given after a run was prepared counts at its launch
    (d / "inv.sch").unlink()
    _cell(d, "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s1", ".include /etc/hostname")])
    run = srv.prepare_lvs("p", "l", "top")
    trust.set_trusted(srv.project("p"), "ip@229914698b1f", True)
    assert run.launch() == "job"


def test_the_signoff_suite_returns_an_lvs_trust_refusal_with_its_code_and_findings(tmp_path, monkeypatch):
    srv = _server(tmp_path, _fake_sky130(tmp_path))
    d = _install(srv)
    _cell(d, "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s1", ".include /etc/hostname")])
    _cell(srv.project("p").library("l").path, "top", _sub("ip/inv.sym", "x1"))
    db = srv.db("p", "l")
    with db.lock:
        db.layout.create_cell("top")
    monkeypatch.setattr(apimod.verify, "start_lvs", lambda *a, **k: pytest.fail("LVS started"))
    monkeypatch.setattr(srv, "suite", lambda *a: [("KLayout LVS", srv.prepare_lvs("p", "l", "top"))])
    r = TestClient(create_app(srv, None)).post("/api/p/p/l/l/tapeout/top/suite", json={"reuse": False})
    assert r.status_code == 200, r.text
    [job] = r.json()["jobs"]
    assert job["name"] == "KLayout LVS" and job["code"] == "untrusted" and "not trusted" in job["error"]
    assert (job["params"]["name"], job["params"]["key"]) == ("ip", "ip@229914698b1f")
    assert [(f["file"], f["kind"]) for f in job["params"]["findings"]] == [("ip/inv.sch", "path")]


# ------------------------------------------------------------------ Xschem


def test_the_xschem_route_refuses_untrusted_tcl_whatever_the_prepass_makes_of_it(tmp_path):
    srv = _server(tmp_path)
    d = _install(srv)
    (d / "tcl.sym").write_text(_tcl_sym(tmp_path / "by_tcl"))
    _cell(d, "inv", ["C {nosuch.sym} 0 0 0 0 {name=x9}", "C {tcl.sym} 0 300 0 0 {name=t1}"])   # the pre-pass fails
    _tb(srv, _sub("ip/inv.sym", "x1"))
    c = TestClient(create_app(srv, None))
    r = c.get("/api/p/p/l/l/c/tb/netlist", params={"backend": "xschem"})
    assert r.status_code == 403 and r.json()["code"] == "untrusted"
    assert [(f["file"], f["kind"]) for f in r.json()["findings"]] == [("ip/tcl.sym", "tcl")]
    assert not (tmp_path / "by_tcl").exists()                 # Xschem never ran

    # a .control block is ngspice's business, not Xschem's: the route does not refuse it
    (d / "tcl.sym").unlink()
    (d / "inv.sch").unlink()
    _cell(d, "inv", [*_r("devices/res.sym", "R1", "1k"), _code("s1", CONTROL)])
    r = c.get("/api/p/p/l/l/c/tb/netlist", params={"backend": "xschem"})
    assert r.status_code != 403 and r.json().get("code") != "untrusted", r.text


# ------------------------------------------------------------------ Verilator


def test_rtl_of_an_untrusted_dependency_is_refused_by_simulation_and_lint_until_trusted(tmp_path, monkeypatch):
    srv = _server(tmp_path)
    d = _install(srv)
    prj = srv.project("p")
    (d / "tb.sv").write_text('module tb;\n  initial begin\n    $c("system(\\"touch x\\");");\n  end\nendmodule\n')
    monkeypatch.setattr(rtl, "verilator", lambda: shutil.which("false") or "/bin/false")   # never builds anything
    e = _refused(lambda: rtl.submit_sim(srv.jobs, prj.path, d, "tb", "t"))
    assert [(f, x.kind, x.line) for f, x in e.findings] == [("ip/tb.sv", "rtl_c", 3)]
    assert e.cell == "ip/tb.sv" and _runs(srv) == []
    assert rtl.lint(d, "tb")["top"] == "tb"                   # lint compiles nothing: only `include paths count

    (d / "tb.sv").write_text('`systemc_header\n#include <stdlib.h>\n`verilog\nmodule tb;\nendmodule\n')
    assert [x.kind for _, x in _refused(lambda: rtl.submit_sim(srv.jobs, prj.path, d, "tb", "t")).findings] == \
        ["systemc"]
    (d / "tb.sv").write_text('`include "/etc/passwd"\nmodule tb;\nendmodule\n')
    assert [x.kind for _, x in _refused(lambda: rtl.lint(d, "tb")).findings] == ["rtl_path"]

    trust.set_trusted(prj, trust.key_of(d), True)
    rtl.lint(d, "tb")
    _done(rtl.submit_sim(srv.jobs, prj.path, d, "tb", "t"))
    assert len(_runs(srv)) == 1
    # a project library is the project's own
    (prj.library("l").path / "tb.sv").write_text('module tb;\n  initial $c("x");\nendmodule\n')
    trust.check_rtl(prj.library("l").path, full=True)


# ------------------------------------------------------------------ the record


def test_a_changeset_worktree_sees_trust_given_in_the_project_after_it_opened(tmp_path):
    srv = _server(tmp_path)
    d = _install(srv)
    (d / "tb.sv").write_text('module tb;\n  initial $c("x");\nendmodule\n')
    prj = srv.project("p")
    work = srv.store.root / ".changesets" / "p" / "c1" / "work"
    shutil.copytree(prj.path, work, ignore=shutil.ignore_patterns("history"))
    key = trust.key_of(work / deps.DEPS / "ip")
    assert key == "ip@229914698b1f" and key not in trust.trusted_keys(work)
    _refused(lambda: trust.check_rtl(work / deps.DEPS / "ip", full=True))
    assert set(trust.untrusted_dirs(work)) == {work / deps.DEPS / "ip"}

    trust.set_trusted(prj, key, True)                         # in the project, after the worktree was made
    assert key in trust.trusted_keys(work) and trust.untrusted_dirs(work) == {}
    trust.check_rtl(work / deps.DEPS / "ip", full=True)
    with pytest.raises(ValueError, match="not in a changeset preview"):
        trust.set_trusted(Project(work, {}), key, False)


def test_keys_trust_entries_and_drop(tmp_path):
    srv = _server(tmp_path)
    prj = srv.project("p")
    d = _install(srv, manifest=MANIFEST)
    assert trust.key_of(d) == "analog_ip@1.2.0"
    (d / "undine-package.toml").write_text("not toml [")             # unreadable: the commit names it
    assert trust.key_of(d) == "ip@229914698b1f"
    (d / cas.MARKER).unlink()
    assert trust.key_of(d) == "ip@unknown"
    for bad in ("ip", "@1.0", "ip@", "../x@1", "ip@1 2"):
        with pytest.raises(ValueError, match="is not a trust key"):
            trust.set_trusted(prj, bad, True)
    with prj.editing():
        prj.meta["trusted"] = ["analog_ip@1.2.0", 7, "nonsense", "ip@229914698b1f", "other@2.0.0"]
        prj.save()
    assert trust.trusted_keys(prj.path) == {"analog_ip@1.2.0", "ip@229914698b1f", "other@2.0.0"}
    meta = json.loads((prj.path / "project.json").read_text())
    assert trust.drop(meta, ["analog_ip", "ip"]) == ["analog_ip@1.2.0", "ip@229914698b1f"]
    assert meta["trusted"] == [7, "nonsense", "other@2.0.0"]
    assert trust.drop(meta, ["nothing"]) == [] and trust.drop({}, ["ip"]) == []
    meta = {"trusted": ["ip@1"]}
    assert trust.drop(meta, ["ip"]) == ["ip@1"] and "trusted" not in meta


def test_a_linked_deps_directory_is_refused(tmp_path):
    srv = _server(tmp_path)
    prj = srv.project("p")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "ip").mkdir(parents=True)
    (prj.path / ".undine").mkdir(exist_ok=True)
    (prj.path / deps.DEPS).symlink_to(elsewhere, target_is_directory=True)
    assert trust.untrusted_dirs(prj.path) == {}               # nothing declared: nothing there is on the symbol path
    with prj.editing():
        prj.meta["libraries"]["ip"] = {"git": "https://example.invalid/ip.git", "rev": "v1"}
        prj.save()
    with pytest.raises(deps.DepError, match="is a link"):
        trust.untrusted_dirs(prj.path)
    with pytest.raises(deps.DepError, match="is a link"):
        trust.check_rtl(prj.path / deps.DEPS / "ip", full=True)


def test_untrusting_in_the_project_reaches_an_open_changeset(tmp_path):
    """A worktree's project.json is a copy from when the changeset opened: it grants nothing once the project takes
    the trust back."""
    srv = _server(tmp_path)
    d = _install(srv)
    (d / "tb.sv").write_text('module tb;\n  initial $c("x");\nendmodule\n')
    prj = srv.project("p")
    key = trust.key_of(d)
    trust.set_trusted(prj, key, True)
    work = srv.store.root / ".changesets" / "p" / "c1" / "work"
    shutil.copytree(prj.path, work, ignore=shutil.ignore_patterns("history"))
    assert key in json.loads((work / "project.json").read_text())["trusted"]
    assert key in trust.trusted_keys(work)

    trust.set_trusted(prj, key, False)
    assert key not in trust.trusted_keys(work)
    assert set(trust.untrusted_dirs(work)) == {work / deps.DEPS / "ip"}
    _refused(lambda: trust.check_rtl(work / deps.DEPS / "ip", full=True))


def test_a_project_with_no_dependency_simulates_with_a_linked_undine(tmp_path):
    """Phase 1 never looked at .undine for a simulation: with no git dependency declared, a linked .undine holds
    nothing on the symbol path, and the gates pass. A declared one behind it is still refused."""
    if not tools.find("ngspice"):
        pytest.skip("needs ngspice")
    srv = _server(tmp_path)
    prj = srv.project("p")
    shutil.move(prj.path / ".undine", tmp_path / "elsewhere") if (prj.path / ".undine").exists() else \
        (tmp_path / "elsewhere").mkdir()
    (prj.path / ".undine").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    _tb(srv, [*_r("devices/res.sym", "R1", "1k"), _code("s1", CONTROL)])
    _done(srv.start_sim("p", "l", "tb", {"type": "op"}))
    assert len(_runs(srv)) == 1
    c = TestClient(create_app(srv, None))
    r = c.get("/api/p/p/l/l/c/tb/netlist", params={"backend": "xschem"})
    assert r.json().get("code") != "untrusted" and "is a link" not in r.text, r.text

    with prj.editing():
        prj.meta["libraries"]["ip"] = {"git": "https://example.invalid/ip.git", "rev": "v1"}
        prj.save()
    with pytest.raises(deps.DepError, match="is a link"):
        srv.start_sim("p", "l", "tb", {"type": "op"})


def test_rtl_and_tcl_refusals_say_line_after_one_space(tmp_path):
    srv = _server(tmp_path)
    d = _install(srv)
    (d / "tb.sv").write_text('module tb;\n  initial $c("x");\nendmodule\n')
    e = _refused(lambda: trust.check_rtl(d, full=True))
    assert str(e).startswith('ip/tb.sv (dependency git 229914698b1f, not trusted): line 2: initial $c("x");')
    (d / "tb.sv").unlink()
    (d / "tcl.sym").write_text(_tcl_sym(tmp_path / "by_tcl"))
    e = _refused(lambda: trust.check_tcl(trust.untrusted_dirs(srv.project("p").path)))
    assert "not trusted): line 3: format=" in str(e) and "):  line" not in str(e)


def test_a_dependency_pin_name_carrying_cards_is_refused(tmp_path):
    """A symbol's pin name reaches the .subckt line of the cell it is formed for: a dependency's pins are checked
    like its other lines, and a pin name with whitespace is refused when the netlist is formed."""
    srv = _server(tmp_path)
    d = _install(srv)
    _cell(d, "inv", _r("devices/res.sym", "R1", "1k"))
    _tb(srv, _sub("ip/inv.sym", "x1"))
    sym = (d / "inv.sym").read_text()
    pins = [ln for ln in sym.splitlines() if ln.startswith("B 5")]
    assert pins, sym
    bad = pins[-1].replace("name=", 'name="y\n.control\nshell x\n.endc" oldname=', 1)
    (d / "inv.sym").unlink()
    (d / "inv.sym").write_text(sym.replace(pins[-1], bad))
    with pytest.raises(ValueError, match="pin"):
        srv.start_sim("p", "l", "tb", {"type": "op"})
    assert _runs(srv) == []


# ------------------------------------------------------------------ LibreLane


def test_a_librelane_flow_whose_top_is_an_untrusted_dependency_is_refused(tmp_path, monkeypatch):
    """LibreLane's Verilator lint and Yosys read the top library's RTL: a dependency's absolute `include is refused
    by run_librelane and by the HTTP route, before any run directory, until a person trusts it."""
    from undine.agents import agent_tools as T
    from undine.design import librelane
    srv = _server(tmp_path)
    d = _install(srv)
    (d / "core.v").write_text('`include "/etc/hostname"\nmodule core(input a, output y);\nendmodule\n')
    prj = srv.project("p")
    fake_pdk = type("P", (), {"name": "sky130A", "root": tmp_path / "pdk" / "sky130A", "id": "sky130A@test",
                              "family": "sky130", "version": "test", "lyp": None, "xschem_dir": None})()
    monkeypatch.setattr(type(prj), "pdk", property(lambda self: fake_pdk))
    monkeypatch.setattr(librelane, "requirements", lambda *a, **k: [])
    monkeypatch.setattr(librelane, "derive", lambda *a, **k: {"sources": [], "config": {}, "pin_order": "",
                                                               "derived": [], "generated": {}, "ignored": []})
    started = []
    monkeypatch.setattr(librelane, "_run", lambda *a: started.append(a))
    librelane.save_flow(prj.path, "core", {"top": {"lib": "ip", "cell": "core"}})

    c = TestClient(create_app(srv, None))
    r = c.post("/api/p/p/flows/core/run")
    assert r.status_code == 403 and r.json()["code"] == "untrusted", r.text
    assert [(f["file"], f["kind"]) for f in r.json()["findings"]] == [("ip/core.v", "rtl_path")]
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    with pytest.raises(T.ToolError, match="not trusted.*an agent cannot"):
        T.run_librelane(ctx, {"flow": "core"})
    assert _runs(srv) == [] and not started

    trust.set_trusted(prj, trust.key_of(d), True)
    assert c.post("/api/p/p/flows/core/run").status_code == 200
    assert len(_runs(srv)) == 1
