"""Phase 3 of docs/design/packages.md, UI: the library browser's Packages section over HTTP (store/packages.py).
Search, a package's versions with the PDK fit and the locked version, refreshing an index, adding a package from an
index by version, and the newer version on a dependency row. Every index and repository is local, under the test's
temporary directory, which the server takes as --dep-root."""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from test_pkg_index import World, _project  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store import deps, packages  # noqa: E402
from undine.store import index as ix  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def _client(tmp_path: Path, **kw) -> TestClient:
    return TestClient(create_app(Server(tmp_path / "projects", {}, [], dep_roots=[tmp_path], **kw), None))


def _world(tmp_path: Path) -> World:
    w = World(tmp_path)
    w.default()
    w.publish("amp", "1.0.0", description="a two-stage amplifier")
    w.publish("amp", "1.1.0", description="a two-stage amplifier")
    w.publish("amp", "2.0.0", yanked=True, description="a two-stage amplifier")
    w.publish("bias", "0.3.0", pdk_line={"family": "gf180mcu", "variants": ["gf180mcuD"]})
    return w


def test_search_lists_the_newest_live_version_of_each_package(tmp_path):
    _world(tmp_path)
    _project(tmp_path)
    c = _client(tmp_path)
    r = c.get("/api/packages", params={"q": "", "project": "p"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["index_name"] == "undine" and len(body["commit"]) == 40
    rows = {x["name"]: x for x in body["packages"]}
    assert rows["amp"]["version"] == "1.1.0" and rows["amp"]["license"] == "Apache-2.0"
    assert rows["bias"]["fits"] == {"ok": False, "reason": "this project has no PDK"}
    assert rows["amp"]["fits"] == {"ok": True, "reason": None}
    assert [x["name"] for x in c.get("/api/packages", params={"q": "two-stage"}).json()["packages"]] == ["amp"]
    assert c.get("/api/packages", params={"q": "nothing"}).json()["packages"] == []


def test_info_gives_every_version_and_the_locked_one(tmp_path):
    _world(tmp_path)
    _, prj = _project(tmp_path)
    c = _client(tmp_path)
    r = c.get("/api/packages/amp", params={"project": "p"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [v["version"] for v in body["versions"]] == ["2.0.0", "1.1.0", "1.0.0"]
    assert body["versions"][0]["yanked"] and body["newest"] == "1.1.0" and body["locked"] is None
    assert body["versions"][1]["deps"] == [] and len(body["versions"][1]["tree"]) == 71
    # added from the browser, by version: the requirement is written as given
    r = c.post("/api/p/p/deps", json={"name": "amp", "package": "amp", "version": "~1.0"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "1.0.0"
    assert c.get("/api/packages/amp", params={"project": "p"}).json()["locked"] == "1.0.0"
    r = c.get("/api/packages/nope")
    assert r.status_code == 404 and "lists no package nope" in r.json()["detail"]


def test_a_dependency_row_shows_the_newer_version_from_the_local_copy(tmp_path):
    w = _world(tmp_path)
    _project(tmp_path)
    c = _client(tmp_path)
    assert c.post("/api/p/p/deps", json={"name": "amp", "package": "amp", "version": "~1.0"}).status_code == 200
    row = next(x for x in c.get("/api/p/p/deps").json()["libraries"] if x["name"] == "amp")
    assert row["newer"] == {"accepted": None, "newest": "1.1.0"}         # ~1.0 does not take 1.1
    w.publish("amp", "1.0.1")
    row = next(x for x in c.get("/api/p/p/deps").json()["libraries"] if x["name"] == "amp")
    assert row["newer"]["accepted"] is None                              # not fetched again: the copy as it was
    r = c.post("/api/indexes/undine/refresh")
    assert r.status_code == 200 and r.json()["commit"] == ix.Index(str(w.index)).commit()
    row = next(x for x in c.get("/api/p/p/deps").json()["libraries"] if x["name"] == "amp")
    assert row["newer"] == {"accepted": "1.0.1", "newest": "1.1.0"}
    r = c.post("/api/p/p/deps/amp/update", json={"version": "^1"})
    assert r.status_code == 200, r.text
    row = next(x for x in c.get("/api/p/p/deps").json()["libraries"] if x["name"] == "amp")
    assert row["version"] == "1.1.0" and "newer" not in row


def test_indexes_lists_the_known_ones_and_a_project_s_own(tmp_path):
    _world(tmp_path)
    other = World(tmp_path, "acme-index")
    _project(tmp_path, {"indexes": {"acme": str(other.index)}})
    c = _client(tmp_path)
    got = {x["name"]: x for x in c.get("/api/indexes", params={"project": "p"}).json()["indexes"]}
    assert set(got) == {"undine", "acme"} and got["acme"]["commit"] is None
    assert c.post("/api/indexes/acme/refresh", params={"project": "p"}).status_code == 200
    got = {x["name"]: x for x in c.get("/api/indexes", params={"project": "p"}).json()["indexes"]}
    assert len(got["acme"]["commit"]) == 40


def test_an_index_on_an_unlisted_host_or_outside_dep_root_is_refused_before_any_fetch(tmp_path, monkeypatch):
    _world(tmp_path)
    _project(tmp_path, {"indexes": {"evil": "https://idx.evil.example/undine-index.git"}})
    called = []
    monkeypatch.setattr(ix.Index, "update", lambda self: called.append(self.url))
    c = _client(tmp_path)
    r = c.get("/api/packages", params={"index": "evil", "project": "p"})
    assert r.status_code == 400 and "not from idx.evil.example" in r.json()["detail"]
    r = c.post("/api/indexes/evil/refresh", params={"project": "p"})
    assert r.status_code == 400 and "--dep-host idx.evil.example" in r.json()["detail"]
    assert called == []
    # a local index outside --dep-root: refused as a local git dependency is
    c2 = TestClient(create_app(Server(tmp_path / "projects", {}, [], dep_roots=[tmp_path / "projects"]), None))
    r = c2.get("/api/packages")
    assert r.status_code == 400 and "dep-root" in r.json()["detail"]


def test_an_add_needs_a_repository_or_a_package(tmp_path):
    _project(tmp_path)
    r = _client(tmp_path).post("/api/p/p/deps", json={"name": "x"})
    assert r.status_code == 400 and "give `git`" in r.json()["detail"]


def test_newer_reads_nothing_it_has_not_fetched(tmp_path):
    w = _world(tmp_path)
    _, prj = _project(tmp_path)
    deps.add(prj, "amp", None, package="amp", version="=1.0.0")
    shutil.rmtree(ix.Index(str(w.index)).repo)                         # no local copy: no row, and no fetch
    assert packages.newer(prj) == {}
    assert not ix.Index(str(w.index)).fetched()
