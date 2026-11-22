"""Phase 3 of docs/design/packages.md, Security: Remote hosts. Over HTTP a fetch goes only to the host of an index this
machine configures ($UNDINE_HOME/config.toml, and the default index) or one given with `serve --dep-host`, whatever
URL the request, project.json, a manifest or an index line names; the all-refs fallback is not used for a request
from the browser. Every fetch is stopped past UNDINE_FETCH_MAX bytes, counted while git runs, and leaves nothing.
No test here reaches the network: a remote host is refused before git runs."""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402
from test_pkg_index import World, _commit, _git, _project  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store import cas, deps, gitsrc  # noqa: E402
from undine.store import index as ix  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def _config(text: str) -> None:
    p = Path(ix.home()) / "config.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# ------------------------------------------------------------------ which host a URL reaches


@pytest.mark.parametrize("url, host", [
    ("https://GitHub.com/kadomoto-lab/undine-index.git", "github.com"),
    ("http://user:pw@git.example.org:8080/a.git", "git.example.org"),
    ("ssh://git@gitlab.com:22/a/b.git", "gitlab.com"),
    ("git://git.example.org/a.git", "git.example.org"),
    ("git@github.com:me/analog_ip.git", "github.com"),
    ("git@[git.example.org:2222]:me/a.git", "git.example.org"),
    ("file:///srv/repos/a", None),
    ("/srv/repos/a", None),
    ("~/repos/a", None),
    ("ext::sh -c touch% /tmp/x", None),
    ("fd::17", None),
])
def test_remote_host_reads_the_host_git_would_reach(url, host):
    assert gitsrc.remote_host(url) == host


def test_allowed_hosts_are_the_configured_indexes_and_dep_host_never_a_projects_own():
    assert deps.allowed_hosts() == ["github.com"]                      # the default index
    _config('[indexes]\nacme = "git@git.acme.example:idx.git"\nlocal = "/srv/index"\n')
    assert deps.allowed_hosts(["GitLab.com ", "github.com"]) == ["github.com", "git.acme.example", "gitlab.com"]
    _config('[indexes]\nundine = "https://mirror.example/undine-index.git"\n')      # a mirror replaces the default
    assert deps.allowed_hosts() == ["mirror.example"]


def test_source_refuses_an_unlisted_host_over_http_only(tmp_path):
    spec = {"git": "https://evil.example/a.git"}
    assert deps._source(spec, None, "lib add p a") is None                            # the CLI: any host
    with pytest.raises(deps.DepError) as e:
        deps._source(spec, [tmp_path], "lib add p a https://evil.example/a.git")
    msg = str(e.value)
    assert "fetches only from github.com" in msg and "not from evil.example" in msg
    assert "`--dep-host evil.example`" in msg and "python -m undine lib add p a" in msg
    src = deps._source(spec, [tmp_path], "lib add p a", ["evil.example"])
    assert isinstance(src, gitsrc.HttpRemote) and src == spec["git"]
    assert isinstance(deps._source({"git": "git@github.com:me/a.git"}, [], "x"), gitsrc.HttpRemote)


# ------------------------------------------------------------------ over HTTP


def test_http_add_and_update_refuse_a_host_before_any_fetch(tmp_path, monkeypatch):
    calls = []
    real = gitsrc._fetch_git
    monkeypatch.setattr(gitsrc, "_fetch_git", lambda *a, **k: calls.append(a) or real(*a, **k))
    root = tmp_path
    srv = Server(root / "projects", {}, [root], dep_hosts=["git.example.org"])
    prj = srv.store.create("p")
    before = (prj.path / "project.json").read_text()
    c = TestClient(create_app(srv, None))
    for url in ("https://evil.example/ip.git", "git@evil.example:ip.git", "ssh://evil.example/ip.git"):
        # phase 4: a host not allowed is a fetch for the person to confirm (the dialog warns), never fetched as asked
        r = c.post("/api/p/p/deps", json={"name": "ip", "git": url, "rev": "v1"})
        assert r.status_code == 409 and r.json()["code"] == "needs_fetch", r.text
        assert "evil.example is not among the hosts" in r.json()["detail"], r.text
        assert [(f["url"], f["rev"], f["host"], f["allowed"]) for f in r.json()["plan"]["fetches"]] == \
            [(url, "v1", "evil.example", False)]
    assert calls == [] and (prj.path / "project.json").read_text() == before
    assert not (prj.path / deps.LOCK).exists() or "evil" not in (prj.path / deps.LOCK).read_text()


def test_http_refuses_an_index_a_project_names_on_an_unlisted_host(tmp_path):
    _, prj = _project(tmp_path, {"indexes": {"acme": "https://idx.evil.example/undine-index.git"}})
    with pytest.raises(deps.DepError) as e:
        deps.add(prj, "amp", None, package="amp", index="acme", local_roots=[tmp_path])
    assert "index https://idx.evil.example/undine-index.git" in str(e.value)
    assert "not from idx.evil.example" in str(e.value)
    assert not ix.Index("https://idx.evil.example/undine-index.git").fetched()


def test_http_refuses_a_package_whose_index_line_names_an_unlisted_host(tmp_path):
    w = World(tmp_path)
    w.default()
    w.append({"name": "amp", "vers": "1.0.0", "git": "https://pkgs.evil.example/amp.git", "commit": "a" * 40,
              "tree": "sha256:" + "b" * 64, "deps": [], "license": "MIT"})
    _, prj = _project(tmp_path)
    before = json.dumps(prj.meta, sort_keys=True)
    with pytest.raises(deps.DepError, match="not from pkgs.evil.example"):
        deps.add(prj, "amp", None, package="amp", local_roots=[tmp_path])
    assert json.dumps(prj.meta, sort_keys=True) == before
    assert not gitsrc.repo_path("https://pkgs.evil.example/amp.git").exists()
    # the same index, its packages local and under --dep-root: added as before
    w.publish("op", "1.0.0")
    r = deps.add(prj, "op", None, package="op", local_roots=[tmp_path])
    assert r["version"] == "1.0.0"


# ------------------------------------------------------------------ the all-refs fallback


def test_the_all_refs_fallback_is_for_the_command_line_only(tmp_path):
    repo = tmp_path / "ip"
    first = _commit(repo, {"a.txt": "1"}, "one", "v1")
    _commit(repo, {"a.txt": "2"}, "two")
    short = first[:9]                    # a depth-1 fetch cannot ask for an abbreviated commit: only the fallback finds it
    url = str(repo)
    with cas.store_lock(shared=True):
        with pytest.raises(gitsrc.GitSourceError) as e:
            gitsrc.fetch(url, gitsrc.HttpRemote(url), short)
        assert "does not fetch every branch and tag" in str(e.value) and "command line" in str(e.value)
        assert gitsrc.fetch(url, None, short) == first


# ------------------------------------------------------------------ the size of a fetch


def _big_repo(d: Path, size: int) -> tuple[Path, str]:
    return d, _commit(d, {"big.bin": os.urandom(size).hex()}, "big", "v1")


def test_a_fetch_past_the_limit_is_stopped_and_leaves_nothing(tmp_path, monkeypatch):
    repo, commit = _big_repo(tmp_path / "big", 400_000)          # 800 kB of hex, about 400 kB once packed
    url = str(repo)
    monkeypatch.setattr(gitsrc, "MAX_BYTES", 100_000)
    with cas.store_lock(shared=True):
        with pytest.raises(gitsrc.FetchTooBig) as e:
            gitsrc.fetch(url, None, "v1")
        assert "more than 100000 bytes" in str(e.value) and "UNDINE_FETCH_MAX" in str(e.value)
        assert not gitsrc.repo_path(url).exists()
        with pytest.raises(gitsrc.FetchTooBig):                 # the fallback's fetch is counted too
            gitsrc.fetch(url, None, commit[:9])
        assert not gitsrc.repo_path(url).exists()
        monkeypatch.setattr(gitsrc, "MAX_BYTES", 10_000_000)
        assert gitsrc.fetch(url, None, "v1") == commit
        packs = list((gitsrc.repo_path(url) / "objects" / "pack").glob("*.pack"))
        assert packs and not list((gitsrc.repo_path(url) / "objects" / "pack").glob("tmp_*"))


def test_a_fetch_is_measured_while_git_runs(tmp_path, monkeypatch):
    """A pack still being written (index-pack's tmp_pack_*) counts: git is stopped before it ends."""
    repo = tmp_path / "kept.git"
    _git(tmp_path, "init", "-q", "--bare", str(repo))
    pack = repo / "objects" / "pack"
    script = tmp_path / "slow-git"
    script.write_text(f"#!/bin/sh\nhead -c 300000 /dev/zero > {pack}/tmp_pack_x\nexec sleep 30\n")
    script.chmod(0o755)
    monkeypatch.setattr(gitsrc, "_cmd", lambda repo_, *a: [str(script)])
    with pytest.raises(gitsrc.FetchTooBig):
        gitsrc._fetch_git(repo, "https://example.org/a.git", "x", limit=100_000)
    assert not repo.exists()


def test_an_index_fetch_past_the_limit_is_refused_and_its_copy_removed(tmp_path, monkeypatch):
    w = World(tmp_path)
    _commit(w.index, {"big.bin": os.urandom(300_000).hex()}, "big")
    monkeypatch.setattr(gitsrc, "MAX_BYTES", 100_000)
    idx = ix.Index(str(w.index))
    with pytest.raises(ix.IndexReadError, match="UNDINE_FETCH_MAX"):
        idx.update()
    assert not idx.fetched() and not idx.repo.exists()
    monkeypatch.setattr(gitsrc, "MAX_BYTES", 10_000_000)
    assert idx.update() == _git(w.index, "rev-parse", "HEAD")
