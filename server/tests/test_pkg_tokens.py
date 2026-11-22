"""Phase 4 of docs/design/packages.md, slice 2: the confirmation token for dependency fetches and index refreshes
over HTTP, and dependency fetches as jobs with progress.

Over HTTP a dependency operation fetches from a remote only what a person confirmed: a route answers 409 needs_fetch
with the plan (URL, commit or rev, tree, size, licence, host), the browser asks for a token of kind 'dependency' after
the person's click, and POST /deps/fetch runs the operation as a job. A local repository under --dep-root needs no
token. An index is never fetched by a read (409 index_not_fetched); a refresh needs a token of kind 'index'. The
command line fetches as before.

Nothing here reaches the network: a "remote" is a URL on a made-up host that git rewrites to a local repository under
the test's directory (url.<base>.insteadOf, given through GIT_CONFIG_COUNT), so Undine treats it as remote (its host
is checked, a token is needed) while git reads a local directory."""
import copy
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
import test_changeset as tc  # noqa: E402
from test_pkg_index import World, _commit, _project  # noqa: E402
from undine import tools  # noqa: E402
from undine.pdks import fetchjobs  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store import cas, deps, gitsrc  # noqa: E402
from undine.store.changeset import ChangesetError, Changesets  # noqa: E402
from undine.store import index as ix  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": None, "klayout": {"version": "0.30.12", "from": "pixi"},
       "xschem": None, "ngspice": None, "magic": None, "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": None, "ngspice": None, "magic": None, "netgen": None}
SYM = "v {}\nK {type=subcircuit}\n"
HOST = "git.example.org"                  # an allowed host (--dep-host)
OTHER = "other.example"                   # a host this server does not fetch from without asking


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.setattr(tools, "xschem_library", lambda: None)
    monkeypatch.delenv("UNDINE_OFFLINE", raising=False)


class Remotes:
    """Made-up remote URLs that git rewrites to local repositories (git's url.<base>.insteadOf, through
    GIT_CONFIG_COUNT): Undine sees a remote host, git reads a directory under the test's own."""

    def __init__(self, monkeypatch):
        self.mp, self.map = monkeypatch, {}

    def add(self, url: str, repo: Path) -> str:
        self.map[url] = repo
        self.mp.setenv("GIT_CONFIG_COUNT", str(len(self.map)))
        for i, (u, r) in enumerate(self.map.items()):
            self.mp.setenv(f"GIT_CONFIG_KEY_{i}", f"url.file://{r}.insteadOf")
            self.mp.setenv(f"GIT_CONFIG_VALUE_{i}", u)
        return url


@pytest.fixture
def remotes(monkeypatch) -> Remotes:
    return Remotes(monkeypatch)


def _server(tmp_path: Path, **kw) -> tuple[Server, TestClient]:
    srv = Server(tmp_path / "projects", {}, [], dep_roots=[tmp_path / "local"], dep_hosts=[HOST], **kw)
    return srv, TestClient(create_app(srv, None))


def _plain(d: Path, cells=("inv",), tag: str = "v1", manifest: str | None = None) -> tuple[Path, str]:
    """A plain git library (cells at the top), tagged; with `manifest`, a package."""
    files = {f"{c}.sym": SYM for c in cells}
    if manifest is not None:
        files["undine-package.toml"] = manifest
    return d, _commit(d, files, tag, tag)


def _remote_package(w: World, remotes: Remotes, name: str, version: str, url: str) -> dict:
    """Package `name` `version` in a local repository that the index lists at the remote `url` (with a size and a
    licence), the tree computed from the local copy: the remote's kept repository stays empty."""
    repo, commit = w.package(name, version)
    with cas.store_lock(shared=True):
        gitsrc.fetch(str(repo), None, f"v{version}")
        files = gitsrc.blob_tree(str(repo), commit, None, "include")
    remotes.add(url, repo)
    line = {"name": name, "vers": version, "git": url, "commit": commit, "tree": cas.tree_hash(files), "deps": [],
            "license": "MIT", "size": 4096, "yanked": False}
    w.append(line)
    return line


def _files(prj) -> dict:
    return {n: (prj.path / n).read_text() if (prj.path / n).is_file() else None for n in ("project.json", deps.LOCK)}


def _confirm(c: TestClient, plan: dict, **over):
    body = {"kind": "dependency", "project": plan.get("project", "p"), **plan["request"], "fetches": plan["fetches"],
            **over}
    return c.post("/api/confirmations", json=body)


def _wait(c: TestClient, project: str, job: dict, timeout: float = 60) -> dict:
    end = time.monotonic() + timeout
    while job["state"] in ("queued", "running"):
        assert time.monotonic() < end, job
        time.sleep(0.05)
        r = c.get(f"/api/p/{project}/deps/fetch/{job['id']}")
        assert r.status_code == 200, r.text
        job = r.json()
    return job


def _fetch(c: TestClient, plan: dict, project: str = "p") -> dict:
    """The person's click: the token, then the fetch job, followed to its end."""
    r = _confirm(c, plan)
    assert r.status_code == 200, r.text
    r = c.post(f"/api/p/{project}/deps/fetch", json={"token": r.json()["token"]})
    assert r.status_code == 200, r.text
    return _wait(c, project, r.json()["job"])


def _index_world(tmp_path: Path, remotes: Remotes) -> tuple[World, dict]:
    w = World(tmp_path / "local")
    w.default()
    line = _remote_package(w, remotes, "amp", "1.0.0", f"https://{HOST}/amp.git")
    ix.Index(str(w.index)).update()                   # fetched before (a person's refresh)
    return w, line


# ------------------------------------------------------------------ a remote needs the person's token


def test_dependency_fetch_needs_token(tmp_path, remotes):
    _, line = _index_world(tmp_path, remotes)
    _, prj = _project(tmp_path)
    srv, c = _server(tmp_path)
    before = _files(prj)
    r = c.post("/api/p/p/deps", json={"name": "amp", "package": "amp", "version": "^1"})
    assert r.status_code == 409 and r.json()["code"] == "needs_fetch", r.text
    plan = r.json()["plan"]
    assert plan["project"] == "p" and plan["op"] == "add" and plan["complete"] is True
    assert plan["request"] == {"op": "add", "name": "amp", "package": "amp", "version": "^1"}
    [f] = plan["fetches"]
    assert (f["url"], f["commit"], f["tree"], f["size"], f["license"]) == \
        (line["git"], line["commit"], line["tree"], 4096, "MIT")
    assert (f["rev"], f["package"], f["version"], f["host"], f["allowed"]) == (None, "amp", "1.0.0", HOST, True)
    assert _files(prj) == before and not gitsrc.repo_path(line["git"]).exists()      # nothing fetched or written
    # a plain git dependency named by a rev: the commit is not known before the fetch
    repo, _ = _plain(tmp_path / "ip")
    url = remotes.add(f"https://{HOST}/ip.git", repo)
    r = c.post("/api/p/p/deps", json={"name": "ip", "git": url, "rev": "v1"})
    assert r.status_code == 409, r.text
    [f] = r.json()["plan"]["fetches"]
    assert (f["url"], f["rev"], f["commit"], f["tree"], f["size"]) == (url, "v1", None, None, None)
    assert r.json()["plan"]["complete"] is False                # its manifest may name more, read once fetched
    assert _files(prj) == before and not gitsrc.repo_path(url).exists()
    # the one-click route and a changeset's: refused the same way, the one-click before any changeset is opened
    r = c.post("/api/p/p/deps/changeset", json={"action": "add", "name": "ip", "git": url, "rev": "v1"})
    assert r.status_code == 409 and r.json()["plan"]["request"]["adopt"] is True, r.text
    # local repositories under --dep-root need no token (the 200s of test_api_phase2.py)
    local, _ = _plain(tmp_path / "local" / "lib")
    r = c.post("/api/p/p/deps", json={"name": "loc", "git": str(local), "rev": "v1"})
    assert r.status_code == 200, r.text


def test_token_bound_to_url_commit_tree(tmp_path, remotes):
    _, line = _index_world(tmp_path, remotes)
    _, prj = _project(tmp_path)
    srv, c = _server(tmp_path)
    plan = c.post("/api/p/p/deps", json={"name": "amp", "package": "amp"}).json()["plan"]
    f = plan["fetches"][0]
    for bad in ({**f, "commit": "0" * 40}, {**f, "tree": "sha256:" + "0" * 64},
                {**f, "url": f"https://{HOST}/other.git"}):
        r = _confirm(c, plan, fetches=[bad])
        assert r.status_code == 400 and "show the fetch dialog again" in r.json()["detail"], r.text
    r = _confirm(c, plan, fetches=[])
    assert r.status_code == 400, r.text
    r = _confirm(c, plan, name="ip", git=f"https://{HOST}/ip.git")    # an operation whose plan is not that one
    assert r.status_code == 400, r.text
    with deps._deps_lock(prj.path):              # another operation of the project fetching: the plan does not wait
        r = _confirm(c, plan)
    assert r.status_code == 409 and r.json()["code"] == "busy", r.text
    tok = _confirm(c, plan).json()["token"]
    # undine.lock changed between the confirmation and the fetch: 403, and the token is used up
    lock = deps.read_lock(prj.path)
    deps.write_lock(prj.path, {**lock, "tools": {**lock["tools"], "magic": {"version": "8.3", "from": "pixi"}}})
    r = c.post("/api/p/p/deps/fetch", json={"token": tok})
    assert r.status_code == 403 and "changed since the fetch was confirmed" in r.json()["detail"], r.text
    deps.write_lock(prj.path, lock)
    r = c.post("/api/p/p/deps/fetch", json={"token": tok})
    assert r.status_code == 403 and "used already" in r.json()["detail"], r.text
    assert not gitsrc.repo_path(line["git"]).exists()
    # a token of another project, or of another kind, is refused
    tok = _confirm(c, plan).json()["token"]
    srv.store.create("q")
    assert c.post("/api/p/q/deps/fetch", json={"token": tok}).status_code == 403
    trust_tok, _ = srv.confirmations.issue({"kind": "trust", "project": "p", "name": "amp", "key": "x", "tree": "y"})
    r = c.post("/api/p/p/deps/fetch", json={"token": trust_tok})
    assert r.status_code == 403 and "trusting a dependency, not a dependency fetch" in r.json()["detail"], r.text
    assert c.post("/api/p/p/deps/fetch", json={}).status_code == 403


def test_token_single_use_and_expires(tmp_path, remotes, monkeypatch):
    _, line = _index_world(tmp_path, remotes)
    _, prj = _project(tmp_path)
    srv, c = _server(tmp_path)
    plan = c.post("/api/p/p/deps", json={"name": "amp", "package": "amp"}).json()["plan"]
    tok = _confirm(c, plan).json()["token"]
    r = c.post("/api/p/p/deps/fetch", json={"token": tok})
    assert r.status_code == 200, r.text
    job = _wait(c, "p", r.json()["job"])
    assert job["state"] == "done", job
    assert job["result"]["version"] == "1.0.0" and job["result"]["cells"] == ["amp_cell"]
    assert deps.read_lock(prj.path)["libraries"]["amp"]["tree"] == line["tree"]
    assert (deps.dep_dir(prj.path, "amp") / "amp_cell.sym").is_file()
    r = c.post("/api/p/p/deps/fetch", json={"token": tok})
    assert r.status_code == 403 and "used already" in r.json()["detail"], r.text
    # five minutes: a token past its time is refused (and gone)
    monkeypatch.setattr(fetchjobs, "TTL", -1)
    r = c.post("/api/p/p/deps/amp/remove")
    assert r.status_code == 200, r.text
    plan = c.post("/api/p/p/deps", json={"name": "amp", "package": "amp"}).json()
    assert "plan" not in plan                                  # in the store now: added with no fetch
    repo, _ = _plain(tmp_path / "ip")
    url = remotes.add(f"https://{HOST}/ip.git", repo)
    plan = c.post("/api/p/p/deps", json={"name": "ip", "git": url, "rev": "v1"}).json()["plan"]
    tok = _confirm(c, plan).json()["token"]
    r = c.post("/api/p/p/deps/fetch", json={"token": tok})
    assert r.status_code == 403 and "expired" in r.json()["detail"], r.text


def test_fetch_job_progress_and_kind(tmp_path, remotes):
    _, line = _index_world(tmp_path, remotes)
    _project(tmp_path)
    _, c = _server(tmp_path)
    plan = c.post("/api/p/p/deps", json={"name": "amp", "package": "amp"}).json()["plan"]
    job = _fetch(c, plan)
    assert job["state"] == "done" and job["kind"] == "dependency" and job["project"] == "p", job
    p = job["progress"]
    assert p["url"] == line["git"] and ("files" in p or "bytes" in p), p
    if "files" in p:
        assert p["files"] == p["of"] >= 2                      # the cell and the manifest
    # the PDK's job route does not know a dependency job, nor the other way round
    assert c.get(f"/api/p/p/pdk/fetch/{job['id']}").status_code == 404
    assert c.get("/api/p/p/deps/fetch/nope").status_code == 404


def test_progress_reports_bytes_while_git_fetches(tmp_path, monkeypatch):
    seen = []
    repo, commit = _plain(tmp_path / "ip")
    token = gitsrc.PROGRESS.set(lambda **p: seen.append(p))
    try:
        with cas.store_lock(shared=True):
            gitsrc.fetch(str(repo), None, "v1")
            gitsrc.blob_tree(str(repo), commit, None)
    finally:
        gitsrc.PROGRESS.reset(token)
    assert any("bytes" in p and p["url"] == str(repo) and p["limit"] == gitsrc.MAX_BYTES for p in seen), seen
    assert seen[-1] == {"url": str(repo), "files": 1, "of": 1}


# ------------------------------------------------------------------ indexes


def test_index_refresh_needs_token(tmp_path):
    w = World(tmp_path / "local")
    w.default()
    w.publish("amp", "1.0.0")
    other = World(tmp_path / "local", "acme-index")
    _project(tmp_path, {"indexes": {"acme": str(other.index)}})
    srv, c = _server(tmp_path)
    r = c.post("/api/indexes/undine/refresh")
    assert r.status_code == 403 and "an index refresh needs the person's confirmation" in r.json()["detail"], r.text
    dep_tok, _ = srv.confirmations.issue({"kind": "dependency", "project": "", "request": {}, "fetches": []})
    r = c.post("/api/indexes/undine/refresh", json={"token": dep_tok})
    assert r.status_code == 403 and "not an index refresh" in r.json()["detail"], r.text
    assert not ix.Index(str(w.index)).fetched()
    r = c.post("/api/confirmations", json={"kind": "index", "index": "undine"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == str(w.index) and body["index"] == "undine" and body["host"] is None and body["commit"] is None
    tok = c.post("/api/confirmations", json={"kind": "index", "index": "undine", "project": "p"}).json()["token"]
    r = c.post("/api/indexes/acme/refresh", json={"token": tok}, params={"project": "p"})   # another index
    assert r.status_code == 403 and f"for index {w.index}" in r.json()["detail"], r.text
    assert not ix.Index(str(other.index)).fetched()
    tok = c.post("/api/confirmations", json={"kind": "index", "index": "undine"}).json()["token"]
    r = c.post("/api/indexes/undine/refresh", json={"token": tok}, params={"project": "p"})  # another project
    assert r.status_code == 403, r.text
    tok = c.post("/api/confirmations", json={"kind": "index", "index": "undine"}).json()["token"]
    r = c.post("/api/indexes/undine/refresh", json={"token": tok})
    assert r.status_code == 200 and r.json()["commit"] == ix.Index(str(w.index)).commit(), r.text
    assert c.post("/api/indexes/undine/refresh", json={"token": tok}).status_code == 403     # used up
    # a project's own index, by its nickname there
    r = c.post("/api/confirmations", json={"kind": "index", "index": "acme", "project": "p"})
    assert r.status_code == 200 and r.json()["index"] == "acme", r.text
    r = c.post("/api/indexes/acme/refresh", json={"token": r.json()["token"]}, params={"project": "p"})
    assert r.status_code == 200 and ix.Index(str(other.index)).fetched(), r.text


def test_package_reads_never_fetch_index(tmp_path, monkeypatch):
    w = World(tmp_path / "local")
    w.default()
    w.publish("amp", "1.0.0")
    _, prj = _project(tmp_path)
    _, c = _server(tmp_path)
    for path in ("/api/packages", "/api/packages/amp"):
        r = c.get(path, params={"project": "p"})
        assert r.status_code == 409 and r.json()["code"] == "index_not_fetched", r.text
        assert r.json()["params"] == {"index": "undine", "url": str(w.index), "host": None}
    # nor does an add, an update or a lock that reads it
    before = _files(prj)
    r = c.post("/api/p/p/deps", json={"name": "amp", "package": "amp"})
    assert r.status_code == 409 and r.json()["code"] == "index_not_fetched", r.text
    assert _files(prj) == before and not ix.Index(str(w.index)).fetched()
    prj.meta["libraries"]["amp"] = {"version": "^1"}
    prj.save()
    r = c.post("/api/p/p/deps/lock")
    assert r.status_code == 409 and r.json()["code"] == "index_not_fetched", r.text
    assert not ix.Index(str(w.index)).fetched()
    # once a person refreshed it: an update reads the copy as it is, never fetching it again
    del prj.meta["libraries"]["amp"]
    prj.save()
    ix.Index(str(w.index)).update()
    assert c.post("/api/p/p/deps", json={"name": "amp", "package": "amp"}).status_code == 200
    w.publish("amp", "1.1.0")
    monkeypatch.setattr(ix.Index, "update", lambda self: pytest.fail(f"fetched index {self.url}"))
    r = c.post("/api/p/p/deps/amp/update", json={})
    assert r.status_code == 200 and deps.read_lock(prj.path)["libraries"]["amp"]["version"] == "1.0.0", r.text
    assert c.get("/api/packages/amp").json()["newest"] == "1.0.0"


# ------------------------------------------------------------------ hosts and revs


def test_confirmed_url_outside_allowed_hosts(tmp_path, remotes):
    _, prj = _project(tmp_path)
    _, c = _server(tmp_path)
    repo, commit = _plain(tmp_path / "ip")
    url = remotes.add(f"https://{OTHER}/ip.git", repo)
    r = c.post("/api/p/p/deps", json={"name": "ip", "git": url, "rev": "v1"})
    assert r.status_code == 409, r.text
    plan = r.json()["plan"]
    [f] = plan["fetches"]
    assert (f["host"], f["allowed"]) == (OTHER, False)
    assert f"{OTHER} is not among the hosts" in r.json()["detail"]
    job = _fetch(c, plan)
    assert job["state"] == "done", job
    assert deps.read_lock(prj.path)["libraries"]["ip"]["commit"] == commit
    assert (deps.dep_dir(prj.path, "ip") / "inv.sym").is_file()
    # the confirmation passed only that URL: a lock that needs it again (another rev) asks again
    _commit(repo, {"buf.sym": SYM}, "v2", "v2")
    r = c.post("/api/p/p/deps/ip/update", json={"rev": "v2"})
    assert r.status_code == 409 and r.json()["plan"]["fetches"][0]["rev"] == "v2", r.text
    assert r.json()["plan"]["request"] == {"op": "update", "name": "ip", "rev": "v2"}


def test_git_rev_needs_second_round(tmp_path, remotes):
    _, prj = _project(tmp_path)
    _, c = _server(tmp_path)
    b_url = f"https://{HOST}/b.git"
    brepo, bcommit = _plain(tmp_path / "b", ["b_cell"], manifest='[package]\nname = "b"\nversion = "1.0.0"\n')
    remotes.add(b_url, brepo)
    arepo, acommit = _plain(tmp_path / "a", ["a_cell"], manifest=(
        '[package]\nname = "a"\nversion = "1.0.0"\n\n[dependencies]\n'
        f'b = {{ git = "{b_url}", rev = "v1" }}\n'))
    a_url = remotes.add(f"https://{HOST}/a.git", arepo)
    r = c.post("/api/p/p/deps", json={"name": "a", "git": a_url, "rev": "v1"})
    assert r.status_code == 409, r.text
    plan = r.json()["plan"]
    assert [(f["url"], f["rev"], f["commit"]) for f in plan["fetches"]] == [(a_url, "v1", None)]
    assert plan["complete"] is False
    # the first round fetches a; its manifest names b, which no one confirmed: the job fails with the new plan
    job = _fetch(c, plan)
    assert job["state"] == "failed" and job["code"] == "needs_fetch", job
    plan2 = job["plan"]
    assert [(f["url"], f["rev"], f["commit"]) for f in plan2["fetches"]] == [(a_url, "v1", acommit), (b_url, "v1", None)]
    assert plan2["request"] == plan["request"] and "a" not in prj.library_specs()
    # the plan made now reads a's manifest from what the first round kept: the same plan, so the person confirms it
    r = c.post("/api/p/p/deps", json={"name": "a", "git": a_url, "rev": "v1"})
    assert r.status_code == 409 and r.json()["plan"]["fetches"] == plan2["fetches"], r.text
    job = _fetch(c, plan2)
    assert job["state"] == "done", job
    lock = deps.read_lock(prj.path)
    assert lock["libraries"]["a"]["commit"] == acommit and lock["packages"]["b"]["commit"] == bcommit
    assert (deps.dep_dir(prj.path, "b") / "b_cell.sym").is_file()


def test_a_rev_that_moved_since_it_was_confirmed_is_confirmed_again(tmp_path, remotes):
    _, prj = _project(tmp_path)
    _, c = _server(tmp_path)
    repo, first = _plain(tmp_path / "ip")
    url = remotes.add(f"https://{HOST}/ip.git", repo)
    with cas.store_lock(shared=True):
        gitsrc.fetch(url, None, "v1")                       # fetched once before: the plan shows that commit
    plan = c.post("/api/p/p/deps", json={"name": "ip", "git": url, "rev": "v1"}).json()["plan"]
    assert plan["fetches"][0]["commit"] == first
    tok = _confirm(c, plan).json()["token"]
    _git_retag(repo)
    r = c.post("/api/p/p/deps/fetch", json={"token": tok})
    job = _wait(c, "p", r.json()["job"])
    assert job["state"] == "failed" and job["code"] == "needs_fetch", job
    moved = job["plan"]["fetches"][0]
    assert moved["rev"] == "v1" and moved["commit"] not in (None, first)
    assert "ip" not in prj.library_specs()


def _git_retag(repo: Path) -> None:
    _commit(repo, {"buf.sym": SYM}, "moved")
    subprocess.run(["git", "-C", str(repo), "tag", "-f", "v1"], check=True, capture_output=True)


# ------------------------------------------------------------------ sync, lock and changesets


def test_sync_and_lock_over_http_need_the_token(tmp_path, remotes, monkeypatch):
    _, line = _index_world(tmp_path, remotes)
    _, prj = _project(tmp_path)
    deps.add(prj, "amp", None, package="amp")                 # the command line fetches it
    _, c = _server(tmp_path)
    shutil.rmtree(deps.dep_dir(prj.path, "amp"))              # a fresh clone on another machine
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "store2"))
    ix.Index(str(tmp_path / "local" / "index")).update()
    r = c.post("/api/p/p/deps/sync", json={})
    assert r.status_code == 409, r.text
    plan = r.json()["plan"]
    assert plan["request"] == {"op": "sync"}
    [f] = plan["fetches"]
    assert (f["url"], f["commit"], f["tree"], f["size"], f["license"]) == \
        (line["git"], line["commit"], line["tree"], 4096, "MIT")
    job = _fetch(c, plan)
    assert job["state"] == "done" and job["result"]["libraries"][0]["action"] == "installed", job
    assert (deps.dep_dir(prj.path, "amp") / "amp_cell.sym").is_file()
    # lib lock of a requirement written by hand: the same
    repo, _ = _plain(tmp_path / "ip")
    url = remotes.add(f"https://{HOST}/ip.git", repo)
    prj.meta["libraries"]["ip"] = {"git": url, "rev": "v1"}
    prj.save()
    r = c.post("/api/p/p/deps/lock")
    assert r.status_code == 409 and r.json()["plan"]["request"] == {"op": "lock"}, r.text
    job = _fetch(c, r.json()["plan"])
    assert job["state"] == "done" and [x["name"] for x in job["result"]["resolved"]] == ["ip"], job


def test_changeset_and_one_click_operations_fetch_through_the_token(tmp_path, remotes):
    root, srv, cs = tc.setup()
    srv.dep_roots, srv.dep_hosts = [tmp_path.resolve()], [HOST]
    c = TestClient(create_app(srv, None))
    repo, commit = _plain(tmp_path / "ip", ["buf"])
    url = remotes.add(f"https://{HOST}/ip.git", repo)
    # the one-click Add: refused before any changeset is opened (no git-setup either), then the job adopts it
    r = c.post("/api/p/p/deps/changeset", json={"action": "add", "name": "ip", "git": url, "rev": "v1"})
    assert r.status_code == 409, r.text
    assert not (srv.project("p").path / ".git").exists() and cs.list("p") == []
    job = _fetch(c, r.json()["plan"])
    assert job["state"] == "done" and job["result"]["status"] == "applied", job
    prj = srv.project("p")
    assert deps.read_lock(prj.path)["libraries"]["ip"]["commit"] == commit
    assert (deps.dep_dir(prj.path, "ip") / "buf.sym").is_file()
    # an operation in an open changeset: its plan names the changeset, and the job records it there
    _commit(repo, {"amp.sym": SYM}, "v2", "v2")
    cid = cs.create("p", "move ip", actor="user")["id"]
    r = c.post(f"/api/p/p/changesets/{cid}/deps", json={"action": "update", "name": "ip", "rev": "v2"})
    assert r.status_code == 409 and r.json()["plan"]["request"]["changeset"] == cid, r.text
    job = _fetch(c, r.json()["plan"])
    assert job["state"] == "done" and job["result"]["op"] is not None, job
    work = srv.project(f"p~{cid}")
    assert (deps.dep_dir(work.path, "ip") / "amp.sym").is_file()
    assert not (deps.dep_dir(srv.project("p").path, "ip") / "amp.sym").exists()      # the project as it was
    shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------ the command line


def test_cli_still_fetches(tmp_path, remotes):
    _, prj = _project(tmp_path)
    repo, commit = _plain(tmp_path / "ip")
    url = remotes.add(f"https://{OTHER}/ip.git", repo)       # any host, no token: the command line
    r = deps.add(prj, "ip", url, "v1")
    assert r["commit"] == commit and (deps.dep_dir(prj.path, "ip") / "inv.sym").is_file()
    w = World(tmp_path / "local")
    w.default()
    line = _remote_package(w, remotes, "amp", "1.0.0", f"https://{HOST}/amp.git")
    assert not ix.Index(str(w.index)).fetched()
    r = deps.add(prj, "amp", None, package="amp")              # the index fetched on first read, the package too
    assert r["version"] == "1.0.0" and deps.read_lock(prj.path)["libraries"]["amp"]["tree"] == line["tree"]
    # the HTTP form of the same call, with no confirmation, fetches nothing
    shutil.rmtree(gitsrc.repo_path(url))
    with pytest.raises(deps.NeedsFetch) as e:
        deps.update(prj, "ip", "v1", local_roots=[tmp_path / "local"], hosts=[HOST], allow=deps.Allow())
    assert [f["url"] for f in e.value.plan["fetches"]] == [url]
    assert not gitsrc.repo_path(url).exists()


# ------------------------------------------------------------------ regressions (review of the first version)


def _no_network(monkeypatch) -> list:
    """Records every git fetch into a kept repository or an index copy (gitsrc._fetch_git), calling through."""
    calls = []
    real = gitsrc._fetch_git
    monkeypatch.setattr(gitsrc, "_fetch_git", lambda repo, url, *a, **k: calls.append(url) or real(repo, url, *a, **k))
    return calls


def _damage(prj, name: str) -> None:
    """Writes into an installed file through its hard link: the store's object no longer hashes to its name."""
    f = next(p for p in sorted(deps.dep_dir(prj.path, name).iterdir()) if p.suffix == ".sym")
    f.chmod(0o644)
    f.write_text("damaged\n")


def test_sync_with_a_damaged_store_object_needs_the_token(tmp_path, remotes, monkeypatch):
    # A: an entry whose tree is recorded but one of whose objects is damaged is read again from git; over HTTP a
    # commit only the remote has is a need, never a fetch with no token
    _, line = _index_world(tmp_path, remotes)
    _, prj = _project(tmp_path)
    deps.add(prj, "amp", None, package="amp")                 # the command line fetches it
    _damage(prj, "amp")
    shutil.rmtree(deps.dep_dir(prj.path, "amp"))
    shutil.rmtree(gitsrc.repo_path(line["git"]))              # the kept repository gone (a gc, another machine)
    _, c = _server(tmp_path)
    calls = _no_network(monkeypatch)
    r = c.post("/api/p/p/deps/sync", json={})
    assert r.status_code == 409 and r.json()["code"] == "needs_fetch", r.text
    assert [(f["url"], f["commit"]) for f in r.json()["plan"]["fetches"]] == [(line["git"], line["commit"])]
    assert calls == [] and not deps.dep_dir(prj.path, "amp").exists()
    job = _fetch(c, r.json()["plan"])                          # confirmed: read again from the remote
    assert job["state"] == "done", job
    assert calls == [line["git"]]
    assert (deps.dep_dir(prj.path, "amp") / "amp_cell.sym").read_text() == SYM


def test_a_commit_gone_between_check_and_read_is_a_need_not_a_fetch(tmp_path, remotes, monkeypatch):
    # B: has_commit said kept, then the kept repository lost it: the later read never reaches the network
    _, line = _index_world(tmp_path, remotes)
    _, prj = _project(tmp_path)
    with cas.store_lock(shared=True):
        gitsrc.fetch(line["git"], None, line["commit"], line["commit"])     # kept: a plan needs no fetch
    _, c = _server(tmp_path)
    calls = _no_network(monkeypatch)
    real = gitsrc.has_commit
    seen = []

    def vanishing(url, commit):                                # kept at the first look, gone after it
        if url == line["git"] and not seen:
            seen.append(commit)
            shutil.rmtree(gitsrc.repo_path(url))
            return True
        return real(url, commit)
    monkeypatch.setattr(gitsrc, "has_commit", vanishing)
    r = c.post("/api/p/p/deps", json={"name": "amp", "package": "amp"})
    assert seen and r.status_code == 409 and r.json()["code"] == "needs_fetch", r.text
    assert calls == [] and "amp" not in prj.library_specs()
    # sync: the entry was there when looked at, gone when fetched
    monkeypatch.setattr(gitsrc, "has_commit", real)
    deps.add(prj, "amp", None, package="amp")
    shutil.rmtree(deps.dep_dir(prj.path, "amp"))
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "store2"))     # its files not in this store
    ix.Index(str(tmp_path / "local" / "index")).update()
    with cas.store_lock(shared=True):
        gitsrc.fetch(line["git"], None, line["commit"], line["commit"])
    calls.clear()
    seen.clear()
    monkeypatch.setattr(gitsrc, "has_commit", vanishing)
    r = c.post("/api/p/p/deps/sync", json={})
    assert seen and r.status_code == 409 and r.json()["code"] == "needs_fetch", r.text
    assert calls == [] and not deps.dep_dir(prj.path, "amp").exists()


def test_a_plan_with_rows_of_one_url_differing_by_a_missing_field_is_confirmed(tmp_path, remotes):
    # C: rows (url, v1, subdir None) and (url, v1, subdir "sub") sort at the confirmation; confirm, fetch, done
    _, prj = _project(tmp_path)
    _, c = _server(tmp_path)
    repo = tmp_path / "mono"
    _commit(repo, {"inv.sym": SYM, "sub/buf.sym": SYM}, "v1", "v1")
    url = remotes.add(f"https://{HOST}/mono.git", repo)
    prj.meta["libraries"] |= {"a": {"git": url, "rev": "v1"}, "b": {"git": url, "rev": "v1", "subdir": "sub"}}
    prj.save()
    r = c.post("/api/p/p/deps/lock")
    assert r.status_code == 409, r.text
    plan = r.json()["plan"]
    assert sorted((f["url"], f["rev"], f["subdir"] or "") for f in plan["fetches"]) == \
        [(url, "v1", ""), (url, "v1", "sub")]
    job = _fetch(c, plan)
    assert job["state"] == "done", job
    lock = deps.read_lock(prj.path)["libraries"]
    assert lock["a"]["commit"] == lock["b"]["commit"]
    assert (deps.dep_dir(prj.path, "b") / "buf.sym").is_file()


def test_one_click_while_the_dependencies_are_busy_opens_nothing(tmp_path, remotes):
    # D: the one-click plan does not wait behind a running operation, and says so before anything is opened
    root, srv, cs = tc.setup()
    srv.dep_roots, srv.dep_hosts = [tmp_path.resolve()], [HOST]
    c = TestClient(create_app(srv, None))
    repo, _ = _plain(tmp_path / "ip", ["buf"])
    url = remotes.add(f"https://{HOST}/ip.git", repo)
    prj = srv.project("p")
    with deps._deps_lock(prj.path):
        r = c.post("/api/p/p/deps/changeset", json={"action": "add", "name": "ip", "git": url, "rev": "v1"})
    assert r.status_code == 409 and r.json()["code"] == "busy", r.text
    assert not (prj.path / ".git").exists() and cs.list("p") == []
    shutil.rmtree(root, ignore_errors=True)


def test_a_queued_job_is_refused_when_an_earlier_one_changed_the_files(tmp_path, remotes):
    # E: two tokens confirmed against the same files; the second job, run after the first wrote them, is refused
    _index_world(tmp_path, remotes)
    _, prj = _project(tmp_path)
    _, c = _server(tmp_path)
    repo, _ = _plain(tmp_path / "ip")
    url = remotes.add(f"https://{HOST}/ip.git", repo)
    p1 = c.post("/api/p/p/deps", json={"name": "amp", "package": "amp"}).json()["plan"]
    p2 = c.post("/api/p/p/deps", json={"name": "ip", "git": url, "rev": "v1"}).json()["plan"]
    t1, t2 = _confirm(c, p1).json()["token"], _confirm(c, p2).json()["token"]
    with deps._deps_lock(prj.path):                            # the first job waits for the lock, the second behind it
        j1 = c.post("/api/p/p/deps/fetch", json={"token": t1}).json()["job"]
        j2 = c.post("/api/p/p/deps/fetch", json={"token": t2}).json()["job"]
    j1, j2 = _wait(c, "p", j1), _wait(c, "p", j2)
    assert j1["state"] == "done", j1
    assert j2["state"] == "failed" and j2["code"] == "files_changed", j2
    assert "changed since the fetch was confirmed" in j2["error"]
    assert "ip" not in prj.library_specs() and not gitsrc.repo_path(url).exists()


# ------------------------------------------------------------------ regressions (verification of the fixes)


def test_a_damaged_locked_entry_is_said_the_same_by_the_route_and_the_plan(tmp_path, remotes, monkeypatch):
    # 1: a locked entry with a damaged object whose commit only the remote has. The first stage takes the bad object
    # out, after which the entry is one not in the store; the route and plan_fetch both say it that way from the
    # start (a plain entry left for sync, which then needs the token), so no plan is one the confirmation refuses
    _, prj = _project(tmp_path)
    repo = tmp_path / "ip"
    commit = _commit(repo, {"inv.sym": SYM + "* ip's own\n"}, "v1", "v1")      # an object no other repository has
    url = remotes.add(f"https://{HOST}/ip.git", repo)
    deps.add(prj, "ip", url, "v1")                            # the command line
    _damage(prj, "ip")
    shutil.rmtree(deps.dep_dir(prj.path, "ip"))
    shutil.rmtree(gitsrc.repo_path(url))
    local, _ = _plain(tmp_path / "local" / "loc", ["buf"])
    srv, c = _server(tmp_path)
    calls = _no_network(monkeypatch)
    args = {"name": "loc", "git": str(local), "rev": "v1"}
    assert deps.plan_fetch(srv.project("p"), "add", args, srv.dep_roots, srv.dep_hosts)["fetches"] == []
    r = c.post("/api/p/p/deps", json=args)
    assert r.status_code == 200, r.text                        # loc added; ip is left for sync, as when not stored
    assert (deps.dep_dir(prj.path, "loc") / "buf.sym").is_file() and not deps.dep_dir(prj.path, "ip").exists()
    r = c.post("/api/p/p/deps/sync", json={})
    assert r.status_code == 409 and [(f["url"], f["commit"]) for f in r.json()["plan"]["fetches"]] == [(url, commit)]
    assert [u for u in calls if u.startswith("https://")] == []      # a local repository needs none
    job = _fetch(c, r.json()["plan"])                          # the confirmation is accepted, the job runs
    assert job["state"] == "done", job
    assert (deps.dep_dir(prj.path, "ip") / "inv.sym").read_text() == SYM + "* ip's own\n"
    # a package so damaged: not_synced from the first stage, as after it (never a fetch nor a plan to refuse)
    _, line = _index_world(tmp_path, remotes)
    deps.add(srv.project("p"), "amp", None, package="amp")
    _damage(prj, "amp")
    shutil.rmtree(deps.dep_dir(prj.path, "amp"))
    shutil.rmtree(gitsrc.repo_path(line["git"]))
    calls.clear()
    for _ in range(2):
        r = c.post("/api/p/p/deps/ip/update", json={"rev": "v1"})
        assert r.status_code == 409 and r.json()["code"] in ("not_synced", "needs_fetch"), r.text
    assert [u for u in calls if u.startswith("https://")] == []      # a local repository needs none


def test_rows_are_compared_by_what_is_fetched_and_a_tree_both_give(tmp_path, remotes):
    # 1: a row without a tree (found when a commit went while it was read) matches the plan's row; two trees that
    # differ do not
    _index_world(tmp_path, remotes)
    _project(tmp_path)
    _, c = _server(tmp_path)
    plan = c.post("/api/p/p/deps", json={"name": "amp", "package": "amp"}).json()["plan"]
    f = plan["fetches"][0]
    assert _confirm(c, plan, fetches=[{**f, "tree": None}]).status_code == 200
    assert _confirm(c, plan, fetches=[{**f, "tree": "sha256:" + "0" * 64}]).status_code == 400
    assert _confirm(c, plan, fetches=[f, f]).status_code == 400            # a row twice is not the plan


def _index_cs_project(tmp_path, remotes):
    """The changeset test project (no git repository yet) with a format 2 lock, and a remote index package."""
    w, line = _index_world(tmp_path, remotes)
    root, srv, cs = tc.setup()
    srv.dep_roots, srv.dep_hosts = [tmp_path.resolve()], [HOST]
    prj = srv.project("p")
    deps.write_lock(prj.path, {**deps.read_lock(prj.path), "format": 2, "pdk": None, "tools": copy.deepcopy(NOW)})
    return w, line, root, srv, cs, TestClient(create_app(srv, None))


def test_a_confirmed_one_click_plans_again_before_it_opens_a_changeset(tmp_path, remotes):
    # 2: the index refreshed between the confirmation and the job: the newer version is a fetch the token does not
    # hold, refused before anything is opened (no git-setup, no discarded changeset)
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    r = c.post("/api/p/p/deps/changeset", json={"action": "add", "name": "amp", "package": "amp", "version": "^1"})
    assert r.status_code == 409, r.text
    tok = _confirm(c, r.json()["plan"]).json()["token"]
    _remote_package(w, remotes, "amp", "1.1.0", line["git"])
    ix.Index(str(w.index)).update()                           # a person's refresh meanwhile
    job = _wait(c, "p", c.post("/api/p/p/deps/fetch", json={"token": tok}).json()["job"])
    assert job["state"] == "failed" and job["code"] == "needs_fetch", job
    assert [f["version"] for f in job["plan"]["fetches"]] == ["1.1.0"]
    assert not (srv.project("p").path / ".git").exists() and cs.list("p") == []
    shutil.rmtree(root, ignore_errors=True)


def test_a_confirmed_one_click_checks_the_files_and_opens_under_one_hold_of_the_lock(tmp_path, remotes, monkeypatch):
    # 3b: the changeset is made from the project while its dependency lock is held (checked there, not before)
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    prj = srv.project("p")
    held = []
    real_create = Changesets.create

    def create(self, *a, **k):
        try:
            with deps._deps_lock(prj.path, wait=False):
                held.append(False)
        except deps.DepsBusy:
            held.append(True)
        return real_create(self, *a, **k)
    monkeypatch.setattr(Changesets, "create", create)
    r = c.post("/api/p/p/deps/changeset", json={"action": "add", "name": "amp", "package": "amp", "version": "^1"})
    job = _fetch(c, r.json()["plan"])
    assert job["state"] == "done", job
    assert held == [True]
    shutil.rmtree(root, ignore_errors=True)


def test_the_relock_fast_path_writes_under_the_lock_it_checked_the_files_in(tmp_path, monkeypatch):
    # 3b: a confirmed lock with nothing to resolve writes the tools under the same hold as the files check
    _, prj = _project(tmp_path)
    held = []
    real = deps._record_env

    def record(p, guard, todo):
        try:
            with deps._deps_lock(p.path, wait=False):
                held.append(False)
        except deps.DepsBusy:
            held.append(True)
        return real(p, guard, todo)
    monkeypatch.setattr(deps, "_record_env", record)
    deps.relock(prj, local_roots=[tmp_path], allow=deps.Allow(files=deps.files_sha(prj.path)))
    assert held == [True]
    with pytest.raises(deps.FilesChanged):
        deps.relock(prj, local_roots=[tmp_path], allow=deps.Allow(files="0" * 64))


def test_a_remove_in_a_changeset_checks_the_files(tmp_path):
    # 3a: a confirmed remove in a changeset's working copy, its files changed while the job waited: refused
    root, srv, cs = tc.setup()
    srv.dep_roots = [tmp_path.resolve()]
    c = TestClient(create_app(srv, None))
    local, _ = _plain(tmp_path / "loc", ["buf"])
    cid = cs.create("p", "a dependency", actor="user")["id"]
    assert c.post(f"/api/p/p/changesets/{cid}/deps", json={"action": "add", "name": "loc", "git": str(local),
                                                            "rev": "v1"}).status_code == 200
    work = srv.project(f"p~{cid}")
    r = c.post("/api/confirmations", json={"kind": "dependency", "project": "p", "op": "remove", "name": "loc",
                                           "changeset": cid, "fetches": []})
    assert r.status_code == 200, r.text
    with deps._deps_lock(work.path):
        job = c.post("/api/p/p/deps/fetch", json={"token": r.json()["token"]}).json()["job"]
        lock = deps.read_lock(work.path)
        deps.write_lock(work.path, {**lock, "tools": {**lock["tools"], "magic": {"version": "8.3", "from": "pixi"}}})
    job = _wait(c, "p", job)
    assert job["state"] == "failed" and job["code"] == "files_changed", job
    assert "loc" in srv.project(f"p~{cid}").library_specs()
    shutil.rmtree(root, ignore_errors=True)


def test_a_job_failure_with_no_code_of_its_own_keeps_its_changeset_and_kind(tmp_path, remotes, monkeypatch):
    # 4: an adopt refused with an error that has no code (ChangesetError): the job gets the status's code and the
    # kept changeset; a refusal's own "kind" survives under params
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)

    def refuse(self, project, cid, actor="user", *a, **k):
        raise ChangesetError(f"cannot apply {cid}: refused here")
    monkeypatch.setattr(Changesets, "apply", refuse)
    r = c.post("/api/p/p/deps/changeset", json={"action": "add", "name": "amp", "package": "amp", "version": "^1"})
    job = _fetch(c, r.json()["plan"])
    assert job["state"] == "failed" and job["code"] == "invalid_request", job
    cid = job["params"]["changeset"]
    assert job["changeset"] == cid and "cannot apply" in job["error"]
    assert next(x for x in cs.list("p") if x["id"] == cid)["status"] == "open"
    jobs = fetchjobs.Jobs()

    def fail(say):
        raise fetchjobs.JobFailed("cannot hold it", "unsupported_library", {"kind": "link_out", "library": "x"})
    j = jobs.start("k", "p", fail, kind="dependency")
    end = time.monotonic() + 10
    while j.state != "failed":
        assert time.monotonic() < end
        time.sleep(0.01)
    pub = j.public()
    assert pub["kind"] == "dependency" and pub["params"] == {"kind": "link_out", "library": "x"}
    shutil.rmtree(root, ignore_errors=True)


def test_a_confirmed_one_click_waits_behind_another_operation(tmp_path, remotes):
    # a confirmed one-click job that starts while another operation holds the project's dependency lock waits for it
    # (plans, checks the files and opens under one hold), and ends applied; the route alone does not wait (busy)
    w, line, root, srv, cs, c = _index_cs_project(tmp_path, remotes)
    prj = srv.project("p")
    body = {"action": "add", "name": "amp", "package": "amp", "version": "^1"}
    plan = c.post("/api/p/p/deps/changeset", json=body).json()["plan"]
    tok = _confirm(c, plan).json()["token"]
    with deps._deps_lock(prj.path):
        assert c.post("/api/p/p/deps/changeset", json=body).json()["code"] == "busy"
        job = c.post("/api/p/p/deps/fetch", json={"token": tok}).json()["job"]
        time.sleep(0.5)
        r = c.get(f"/api/p/p/deps/fetch/{job['id']}").json()
        assert r["state"] in ("queued", "running"), r        # waiting, not failed busy
    job = _wait(c, "p", job)
    assert job["state"] == "done" and job["result"]["status"] == "applied", job
    assert (deps.dep_dir(srv.project("p").path, "amp") / "amp_cell.sym").is_file()
    shutil.rmtree(root, ignore_errors=True)
