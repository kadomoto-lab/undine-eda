"""HTTP side of packages phase 1 (server/api.py, pdks/fetchjobs.py): the PDK rescan with no restart and no empty
window, the PDK fetch as a job behind a person's single-use confirmation token, the /deps routes on the store (offline
sync, the frozen refusal, lib lock under the --dep-root rule), the netlist fingerprint of a format 2 dependency, and
no agent-facing route to a confirmation. PDKs are fake trees, ciel is the fake one, the tools are fixed: nothing here
reads ~/.ciel, runs a real tool or uses the network."""
import copy
import json
import os
import re
import shlex
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402
from fake_ciel import make_fake_ciel, read_log  # noqa: E402
from test_pdk_ops import SKY, SKY2, _tree  # noqa: E402
from test_store_sync import _repo  # noqa: E402
from undine import tools  # noqa: E402
from undine.pdks import ciel, fetchjobs, pdkops, pin  # noqa: E402
from undine.pdks.pdk import discover  # noqa: E402
from undine.schematics import xschem  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store import deps  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": None, "klayout": {"version": "0.30.12", "from": "pixi"},
       "xschem": None, "ngspice": None, "magic": None, "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": None, "ngspice": None, "magic": None, "netgen": None}
needs_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.setattr(tools, "find", lambda n: f"/opt/t/bin/{n}" if NOW[n] else None)
    monkeypatch.delenv("UNDINE_OFFLINE", raising=False)


def _id(commit: str) -> str:
    return f"sky130A@{commit[:12]}"


def _server(tmp_path: Path, roots: list[Path], **kw) -> tuple[Server, TestClient]:
    srv = Server(tmp_path / "projects", discover(roots), [tmp_path], llm_policy="none", pdk_roots=roots, **kw)
    return srv, TestClient(create_app(srv, None))


def _wait(cond, what: str, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


def _lock_is_free(srv: Server) -> bool:
    """Whether another thread can take the server's lock now (an RLock: the asking thread must not be its holder)."""
    got = []
    t = threading.Thread(target=lambda: got.append(srv.lock.acquire(timeout=2) and (srv.lock.release() or True)))
    t.start()
    t.join()
    return got == [True]


def _empty_schematic(prj, lib: str, cell: str) -> None:
    prj.create_library(lib)
    (prj.path / "libs" / lib / f"{cell}.sch").write_text(
        xschem.dump(xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n")))


def _counting_netlists(srv: Server, monkeypatch) -> list:
    """Counts reference_netlist runs: netlist_sha runs it again only when its fingerprint changed."""
    runs = []
    real = srv.reference_netlist

    def counted(*a, **k):
        runs.append(1)
        return real(*a, **k)
    monkeypatch.setattr(srv, "reference_netlist", counted)
    return runs


# ------------------------------------------------------------------ rescan


def test_rescan_finds_a_pdk_added_after_start_and_an_open_project_resolves_it(tmp_path):
    base = tmp_path / "pdks"
    _tree(base, SKY)
    srv, c = _server(tmp_path, [base])
    kept = srv.pdks[_id(SKY)]
    assert c.post("/api/projects", json={"name": "p", "pdk": _id(SKY)}).status_code == 200
    assert c.post("/api/projects", json={"name": "q"}).status_code == 200
    q = srv.project("q")
    q.meta["pdk"] = _id(SKY2)                                              # asks for a PDK not installed yet
    q.save()
    assert c.post("/api/p/q/libs", json={"name": "lib"}).status_code == 200
    assert c.get("/api/p/q").json()["pdk"] is None
    assert srv.db("q", "lib").pdk is None                                  # an open layout, with no PDK
    assert c.post("/api/p/p/libs", json={"name": "lib"}).status_code == 200
    p_db = srv.db("p", "lib")
    srv.netlist_shas[("x",)] = ([], "stale")

    _tree(base, SKY2)                                                      # installed while the server runs
    r = c.post("/api/pdks/rescan")
    assert r.status_code == 200, r.text
    assert {x["id"] for x in r.json()["pdks"]} == {_id(SKY), _id(SKY2)}
    assert {x["id"] for x in c.get("/api/pdks").json()} == {_id(SKY), _id(SKY2)}
    assert c.get("/api/p/q").json()["pdk"]["id"] == _id(SKY2)              # no restart
    assert ("q", "lib") not in srv.dbs and srv.db("q", "lib").pdk is srv.pdks[_id(SKY2)]
    assert srv.pdks[_id(SKY)] is kept                                       # unchanged: the same object
    assert srv.dbs[("p", "lib")] is p_db                                    # its PDK did not move: kept open
    assert srv.netlist_shas == {}

    shutil.rmtree(base / "ciel/sky130/versions" / SKY2)                     # removed: gone after a rescan
    assert [x["id"] for x in c.post("/api/pdks/rescan").json()["pdks"]] == [_id(SKY)]
    assert c.get("/api/p/q").json()["pdk"] is None and ("q", "lib") not in srv.dbs
    assert srv.pdks[_id(SKY)] is kept


def test_a_pdk_first_scanned_half_extracted_is_replaced_once_its_files_are_there(tmp_path):
    # a rescan (another fetch job finishing, or the badge's Rescan) ran while ciel had made libs.tech but not yet
    # libs.tech/klayout or libs.tech/xschem
    base = tmp_path / "pdks"
    srv, c = _server(tmp_path, [base])
    v = _tree(base, SKY)
    srv.rescan_pdks()
    half = srv.pdks[_id(SKY)]
    assert half.lyp is None and half.xschem_dir is None and half.klayout_tech is None
    (v / "libs.tech/klayout/tech").mkdir(parents=True)
    (v / "libs.tech/klayout/tech/sky130A.lyp").write_text("<layer-properties/>\n")
    (v / "libs.tech/xschem").mkdir()
    srv.rescan_pdks()                                                      # the same id at the same root
    whole = srv.pdks[_id(SKY)]
    assert whole is not half
    assert whole.lyp == v / "libs.tech/klayout/tech/sky130A.lyp" and whole.xschem_dir == v / "libs.tech/xschem"
    assert whole.klayout_tech == v / "libs.tech/klayout"
    srv.rescan_pdks()
    assert srv.pdks[_id(SKY)] is whole                                     # unchanged since: the same object


def test_rescan_racing_readers_never_shows_an_empty_dict_or_loses_an_unchanged_id(tmp_path):
    base = tmp_path / "pdks"
    _tree(base, SKY)
    srv, c = _server(tmp_path, [base])
    keep = srv.pdks[_id(SKY)]
    srv.store.create("p", _id(SKY))
    stop, errors, reads = threading.Event(), [], [0]

    def read():
        try:
            while not stop.is_set():
                prj = srv.project("p")
                if prj.pdk is not keep:
                    errors.append(f"p resolved to {prj.pdk}")
                if not any(p is keep for p in list(srv.pdks.values())):
                    errors.append("the unchanged PDK was missing")
                reads[0] += 1
        except Exception as e:                                             # noqa: BLE001: any is a failure here
            errors.append(repr(e))
    readers = [threading.Thread(target=read) for _ in range(3)]
    for t in readers:
        t.start()
    try:
        for i in range(30):
            if i % 2 == 0:
                _tree(base, SKY2, variant=f"sky130{'AB'[i % 4 // 2]}")
            else:
                shutil.rmtree(base / "ciel/sky130/versions" / SKY2)
            srv.rescan_pdks()
    finally:
        stop.set()
        for t in readers:
            t.join()
    assert errors == [] and reads[0] > 0
    assert srv.pdks[_id(SKY)] is keep


# ------------------------------------------------------------------ PDK fetch: token and job


@pytest.fixture
def blocking_ciel(tmp_path, monkeypatch):
    """The fake ciel behind a gate: each fetch leaves a file in `started` and waits for `go` to exist."""
    log = tmp_path / "ciel.log"
    fake = make_fake_ciel(tmp_path / "fake", [SKY, SKY2], log=log)
    gate = tmp_path / "gate"
    (gate / "started").mkdir(parents=True)
    exe = tmp_path / "bin" / "ciel"
    exe.parent.mkdir()
    exe.write_text(f"""#!/bin/sh
case " $* " in *" fetch "*)
  mktemp {shlex.quote(str(gate / 'started'))}/run.XXXXXX >/dev/null; i=0
  while [ ! -e {shlex.quote(str(gate / 'go'))} ] && [ $i -lt 1200 ]; do sleep 0.05; i=$((i+1)); done;;
esac
exec {shlex.quote(str(fake))} "$@"
""")
    exe.chmod(0o755)
    monkeypatch.setenv("UNDINE_CIEL", str(exe))

    class Gate:
        def started(self) -> int:
            return len(list((gate / "started").iterdir()))

        def open(self) -> None:
            (gate / "go").touch()
        log_path = log
    return Gate()


def _pinned_elsewhere(tmp_path: Path, monkeypatch, libs=("sky130_fd_pr",)) -> Path:
    """A project pinned (format 2) on a machine that has the PDK, with no PDK on this one; returns its root."""
    first = tmp_path / "first"
    old = os.environ["UNDINE_CIEL"]                                        # the test's ciel, set back after
    monkeypatch.setenv("UNDINE_CIEL", str(make_fake_ciel(tmp_path / "pinner", [SKY])))
    ciel.fetch(first, "sky130", "sky130A", SKY, list(libs))
    monkeypatch.setenv("UNDINE_CIEL", old)
    store = ProjectStore(tmp_path / "projects", discover([first]))
    prj = store.create("p", _id(SKY))
    pdkops.verify(prj, pin=True)
    shutil.rmtree(first)
    return tmp_path / "projects"


def _confirm(c: TestClient, **over) -> dict:
    body = {"kind": "pdk", "project": "p", "commit": SKY, "variant": "sky130A", "libraries": ["sky130_fd_pr"]} | over
    return c.post("/api/confirmations", json=body)


def test_a_pdk_fetch_needs_a_fresh_token_bound_to_what_is_fetched(tmp_path, blocking_ciel, monkeypatch):
    _pinned_elsewhere(tmp_path, monkeypatch)
    here = tmp_path / "here"
    srv, c = _server(tmp_path, [here])
    assert srv.project("p").pdk is None
    st = deps.status(srv.project("p"))
    assert not st["ok"]

    r = c.post("/api/p/p/pdk/fetch", json={})                              # no token
    assert r.status_code == 403 and "confirmation" in r.json()["detail"]
    assert c.post("/api/p/p/pdk/fetch", json={"token": "made-up"}).status_code == 403
    # the dialog's plan, then a token only for exactly that plan
    plan = c.get("/api/p/p/pdk/fetch-plan").json()
    assert plan == {"family": "sky130", "variant": "sky130A", "commit": SKY, "libraries": ["sky130_fd_pr"],
                    "source": ciel.DEFAULT_SOURCE, "root": str(here.absolute())}
    for other in ({"commit": SKY2}, {"libraries": ["sky130_fd_pr", "sky130_fd_sc_hd"]}, {"libraries": None},
                  {"variant": "sky130B"}, {"commit": SKY[:6]}):
        r = _confirm(c, **other)
        assert r.status_code == 400 and "show the fetch dialog again" in r.json()["detail"], (other, r.text)
    assert _confirm(c, kind="dependency").status_code == 400
    assert _confirm(c, project="p~0123456789ab").status_code == 400        # a preview

    # a token bound to another commit (made by hand: the route never issues one) is refused by the fetch
    bad, _ = srv.confirmations.issue({"kind": "pdk", "project": "p", "allow_network": False, **plan, "commit": SKY2})
    r = c.post("/api/p/p/pdk/fetch", json={"token": bad})
    assert r.status_code == 400 and "changed since it was confirmed" in r.json()["detail"]
    # expired
    monkeypatch.setattr(fetchjobs, "TTL", -1)
    old = _confirm(c, commit=SKY[:12]).json()["token"]                     # an id's 12 characters will do
    monkeypatch.setattr(fetchjobs, "TTL", 300)
    r = c.post("/api/p/p/pdk/fetch", json={"token": old})
    assert r.status_code == 403 and "expired" in r.json()["detail"]
    # the lock changed after the token was issued
    tok = _confirm(c).json()["token"]
    lock = deps.read_lock(srv.project("p").path)
    lock["pdk"]["libraries"] = ["sky130_fd_pr", "sky130_fd_sc_hd"]
    deps.write_lock(srv.project("p").path, lock)
    r = c.post("/api/p/p/pdk/fetch", json={"token": tok})
    assert r.status_code == 400 and "libraries" in r.json()["detail"], r.text
    lock["pdk"]["libraries"] = ["sky130_fd_pr"]
    deps.write_lock(srv.project("p").path, lock)
    # a token for another project, and a preview
    tok = _confirm(c).json()["token"]
    assert c.post("/api/p/q/pdk/fetch", json={"token": tok}).status_code == 403
    assert c.post("/api/p/p~0123456789ab/pdk/fetch", json={"token": tok}).status_code == 400
    assert blocking_ciel.started() == 0 and read_log(blocking_ciel.log_path) == []     # ciel never ran

    # a fresh token: the job runs ciel without the server's lock; a second fetch of the same PDK waits for it
    r = _confirm(c)
    assert r.status_code == 200 and r.json()["plan"] == plan and r.json()["expires"] > time.time()
    tok = r.json()["token"]
    first = c.post("/api/p/p/pdk/fetch", json={"token": tok})
    assert first.status_code == 200, first.text
    job = first.json()["job"]["id"]
    _wait(lambda: blocking_ciel.started() == 1, "ciel to start")
    assert _lock_is_free(srv)
    assert c.get("/api/pdks").status_code == 200 and c.get("/api/p/p/deps").status_code == 200
    assert c.get(f"/api/p/p/pdk/fetch/{job}").json()["state"] == "running"
    r = c.post("/api/p/p/pdk/fetch", json={"token": tok})                  # used already
    assert r.status_code == 403 and "used already" in r.json()["detail"]
    second = c.post("/api/p/p/pdk/fetch", json={"token": _confirm(c).json()["token"]}).json()["job"]["id"]
    time.sleep(0.5)
    assert c.get(f"/api/p/p/pdk/fetch/{second}").json()["state"] == "queued" and blocking_ciel.started() == 1
    blocking_ciel.open()
    for j in (job, second):
        _wait(lambda: c.get(f"/api/p/p/pdk/fetch/{j}").json()["state"] in ("done", "failed"), f"job {j}")
        rec = c.get(f"/api/p/p/pdk/fetch/{j}").json()
        assert rec["state"] == "done", rec
        assert rec["result"]["verify"]["state"] == "unverified", rec
        assert any("Fetching sky130" in x for x in rec["log"])
    assert blocking_ciel.started() == 2
    assert c.get("/api/p/p").json()["pdk"]["id"] == _id(SKY)               # rescanned: found with no restart
    assert c.get("/api/p/q/pdk/fetch/" + job).status_code == 404
    assert c.get("/api/p/p/pdk/fetch/nope").status_code == 404


def test_a_pdk_fetch_is_refused_when_only_a_volare_root_is_scanned(tmp_path, blocking_ciel, monkeypatch):
    _pinned_elsewhere(tmp_path, monkeypatch)
    vol = tmp_path / "volare-root"
    (vol / "volare").mkdir(parents=True)
    srv, c = _server(tmp_path, [vol])
    r = _confirm(c)
    assert r.status_code == 400 and re.search(r"no PDK root scanned is a ciel root.*--pdk-root", r.json()["detail"])
    assert c.get("/api/p/p/pdk/fetch-plan").status_code == 400
    assert read_log(blocking_ciel.log_path) == []


# ------------------------------------------------------------------ set-pdk on format 2


def test_set_pdk_on_a_format_2_lock_hashes_without_the_server_lock(tmp_path, monkeypatch):
    base = tmp_path / "pdks"
    _tree(base, SKY)
    _tree(base, SKY2)
    srv, c = _server(tmp_path, [base])
    prj = srv.store.create("p", _id(SKY))
    pdkops.upgrade(prj)
    free = []
    real = pin.pin_object

    def slow(pdk):
        time.sleep(0.3)
        free.append(_lock_is_free(srv))
        return real(pdk)
    monkeypatch.setattr(pin, "pin_object", slow)
    r = c.post("/api/p/p/pdk", json={"pdk": _id(SKY2)})
    assert r.status_code == 200, r.text
    assert r.json() == {"from": _id(SKY), "to": _id(SKY2)} and free == [True]
    lock = deps.read_lock(prj.path)
    assert lock["format"] == 2 and lock["pdk"]["commit"] == SKY2


# ------------------------------------------------------------------ /deps


@needs_git
def test_deps_sync_with_no_body_an_empty_one_and_offline(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "src")
    srv, c = _server(tmp_path, [], dep_roots=[tmp_path / "src"])         # git may be read, so that offline decides
    prj = srv.store.create("p")
    deps.add(prj, "ip", str(repo), "v1", "lib")
    r = c.post("/api/p/p/deps/sync")
    assert r.status_code == 200 and [x["action"] for x in r.json()["libraries"]] == ["ok"], r.text
    assert c.post("/api/p/p/deps/sync", json={}).json()["libraries"][0]["action"] == "ok"

    shutil.rmtree(prj.path / deps.DEPS)
    gitsrc_dir = Path(os.environ["UNDINE_STORE"]) / "git"
    shutil.rmtree(gitsrc_dir)                                              # no repository: only the store's records
    r = c.post("/api/p/p/deps/sync", json={"offline": True})
    assert r.status_code == 200 and [x["action"] for x in r.json()["libraries"]] == ["installed"], r.text

    shutil.rmtree(prj.path / deps.DEPS)
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "empty-store"))     # nothing stored: named, nothing changed
    r = c.post("/api/p/p/deps/sync", json={"offline": True})
    assert r.status_code == 400 and "offline, and not in the store: ip @" in r.json()["detail"], r.text
    assert not (prj.path / deps.DEPS / "ip").exists()

    meta = json.loads((prj.path / "project.json").read_text())             # a pull that changed a requirement
    meta["libraries"]["ip"]["rev"] = "v2"
    (prj.path / "project.json").write_text(json.dumps(meta))
    r = c.post("/api/p/p/deps/sync")
    assert r.status_code == 400 and "lib lock p" in r.json()["detail"] and "nothing was changed" in r.json()["detail"]


@needs_git
def test_deps_lock_resolves_under_the_dep_root_rule_and_outdates_netlist_hashes(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "src")
    srv, c = _server(tmp_path, [])                                         # no --dep-root
    prj = srv.store.create("p")
    _empty_schematic(prj, "mylib", "top")
    meta = json.loads((prj.path / "project.json").read_text())             # declared by hand, not locked
    meta["libraries"]["ip"] = {"git": str(repo), "rev": "v1", "subdir": "lib"}
    (prj.path / "project.json").write_text(json.dumps(meta))
    runs = _counting_netlists(srv, monkeypatch)
    srv.netlist_sha("p", "mylib", "top", "top", "sim")
    srv.netlist_sha("p", "mylib", "top", "top", "sim")
    assert len(runs) == 1

    r = c.post("/api/p/p/deps/lock")
    assert r.status_code == 400 and "--dep-root" in r.json()["detail"], r.text
    assert "ip" not in deps.read_lock(prj.path)["libraries"]

    srv2, c2 = _server(tmp_path, [], dep_roots=[tmp_path / "src"])
    runs2 = _counting_netlists(srv2, monkeypatch)
    srv2.netlist_sha("p", "mylib", "top", "top", "sim")
    r = c2.post("/api/p/p/deps/lock")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["name"] for x in body["resolved"]] == ["ip"] and "issues" in body and body["ok"], body
    assert deps.read_lock(prj.path)["libraries"]["ip"]["sha"]              # format 1: a sha, as today
    srv2.netlist_sha("p", "mylib", "top", "top", "sim")
    assert len(runs2) == 2                                                 # outdated: netlisted again


@needs_git
def test_the_netlist_hash_changes_when_a_format_2_dependency_is_updated(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "src")
    srv, c = _server(tmp_path, [])
    prj = srv.store.create("p")
    deps.write_lock(prj.path, {**deps.read_lock(prj.path), "format": 2, "pdk": None, "tools": copy.deepcopy(NOW)})
    deps.add(prj, "ip", str(repo), "v1", "lib")
    assert "tree" in deps.installed(prj.path, "ip") and "sha" not in deps.installed(prj.path, "ip")
    _empty_schematic(srv.project("p"), "mylib", "top")
    runs = _counting_netlists(srv, monkeypatch)
    srv.netlist_sha("p", "mylib", "top", "top", "sim")
    srv.netlist_sha("p", "mylib", "top", "top", "sim")
    assert len(runs) == 1
    deps.update(srv.project("p"), "ip", "v2")
    srv.netlist_sha("p", "mylib", "top", "top", "sim")
    assert len(runs) == 2


# ------------------------------------------------------------------ nothing agent-facing


def test_no_agent_tool_or_client_method_reaches_a_confirmation_or_a_pdk_fetch():
    from undine.agents import agent_tools

    names = [t["name"] for t in agent_tools.TOOLS]
    assert names and not [n for n in names if "confirm" in n or ("pdk" in n and "fetch" in n) or "token" in n]
    src = Path(__file__).resolve().parents[1] / "undine"
    for f in [*sorted((src / "agents").glob("*.py")), src / "client.py"]:
        text = f.read_text()
        for word in ("/api/confirmations", "pdk/fetch", "fetchjobs", "confirmations.", "pdkops.fetch"):
            assert word not in text, (f.name, word)
    from undine.agents.client import Undine
    assert not [m for m in dir(Undine) if "confirm" in m or "fetch" in m]
