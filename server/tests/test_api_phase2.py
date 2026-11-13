"""HTTP side of packages phase 2 (server/api.py, pdks/fetchjobs.py): the /deps routes return what add, update and
remove report about packages; resolution, placed-name, format and set-pdk refusals come back with their codes and
lists, and `detail` is the command line's message; trust is given only with a single-use confirmation token of kind
'trust' bound to the dependency's key and installed tree; internal cells ([exports]) stay out of the symbol list.

The tools are fixed, git is real and every repository local (under a --dep-root); nothing here reads ~/.undine, runs
a real tool or uses the network."""
import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402
from test_deps_phase2 import MANIFEST, _dep, _git, _manifest, _package, _put  # noqa: E402
from undine import tools  # noqa: E402
from undine.pdks import compat, fetchjobs, pdkops  # noqa: E402
from undine.pdks.pdk import Pdk  # noqa: E402
from undine.server.api import Server, _err, create_app  # noqa: E402
from undine.store import cas, deps, locked  # noqa: E402

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": None, "klayout": {"version": "0.30.12", "from": "pixi"},
       "xschem": None, "ngspice": None, "magic": None, "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": None, "ngspice": None, "magic": None, "netgen": None}
A, B = "a" * 40, "b" * 40
needs_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")
# a schematic whose code block runs ngspice control code: what the trust rule flags
CONTROL_SCH = ('v {xschem version=3.4.5 file_version=1.2}\n'
               'C {devices/code_shown.sym} 0 300 0 0 {name=s1 value=".control\necho hello\n.endc"}\n')


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.setattr(tools, "find", lambda n: f"/opt/t/bin/{n}" if NOW[n] else None)
    monkeypatch.setattr(tools, "xschem_library", lambda: None)
    monkeypatch.delenv("UNDINE_OFFLINE", raising=False)
    monkeypatch.delenv("UNDINE_STORE_LINK", raising=False)


def _server(tmp_path: Path, pdks: dict | None = None) -> tuple[Server, TestClient]:
    srv = Server(tmp_path / "projects", pdks or {}, [tmp_path], llm_policy="none", dep_roots=[tmp_path / "src"])
    return srv, TestClient(create_app(srv, None))


def _format2(prj):
    deps.write_lock(prj.path, {**deps.read_lock(prj.path), "format": 2, "pdk": None, "tools": copy.deepcopy(NOW)})
    return prj


def _meta(prj) -> dict:
    return json.loads((prj.path / "project.json").read_text())


def _files(prj) -> dict[str, bytes]:
    out = {n: (prj.path / n).read_bytes() for n in ("project.json", deps.LOCK) if (prj.path / n).exists()}
    d = prj.path / deps.DEPS
    out.update({f.relative_to(d).as_posix(): f.read_bytes() for f in sorted(d.rglob("*")) if f.is_file()}
               if d.is_dir() else {})
    return out


# ------------------------------------------------------------------ /deps


@needs_git
def test_post_deps_reports_the_transitive_packages_and_a_conflict_is_409_with_both_chains(tmp_path):
    src = tmp_path / "src"
    c = _package(src, "c")
    a = _package(src, "a", deps_={"c": _dep(c)}, extra={"inv.sch": CONTROL_SCH})
    sar = _package(src, "sar_common")
    _package(src, "sar_common", "1.3.0", repo=sar)
    ip = _package(src, "analog_ip", "1.2.0", deps_={"sar_common": _dep(sar, "v1.3.0")})
    srv, cl = _server(tmp_path)
    prj = _format2(srv.store.create("p"))

    r = cl.post("/api/p/p/deps", json={"name": "alib", "git": str(a), "rev": "v1.0.0", "package": "a"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["name"], body["package"], body["version"]) == ("alib", "a", "1.0.0")
    assert [(x["name"], x["package"], x["via"]) for x in body["resolved"]] == [("alib", "a", []), ("c", "c", ["a"])]
    assert body["flagged"]["spice"] >= 1 and body["trusted"] is False
    assert _meta(prj)["libraries"]["alib"]["package"] == "a"
    st = cl.get("/api/p/p/deps").json()
    rows = {x["name"]: x for x in st["libraries"]}
    assert rows["c"]["direct"] is False and rows["c"]["package"] == "c" and rows["alib"]["direct"] is True

    assert cl.post("/api/p/p/deps", json={"name": "sar_common", "git": str(sar), "rev": "v1.0.0"}).status_code == 200
    before = _files(prj)
    r = cl.post("/api/p/p/deps", json={"name": "analog_ip", "git": str(ip), "rev": "v1.2.0"})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "resolution_refused" and [p["code"] for p in body["problems"]] == ["conflict"]
    c10, c13, cip = (_git(sar, "rev-parse", "v1.0.0")[:12], _git(sar, "rev-parse", "v1.3.0")[:12],
                     _git(ip, "rev-parse", "v1.2.0")[:12])
    assert sorted(body["problems"][0]["chains"]) == sorted([
        f"p -> sar_common 1.0.0 (v1.0.0 @ {c10})",
        f"p -> analog_ip 1.2.0 (v1.2.0 @ {cip}) -> sar_common 1.3.0 (v1.3.0 @ {c13})"])
    assert _files(prj) == before
    with pytest.raises(deps.ResolveError) as e:                         # what the command line prints
        deps.add(srv.project("p"), "analog_ip", str(ip), "v1.2.0")
    assert body["detail"] == str(e.value) and body["detail"].endswith("nothing was changed")


@needs_git
def test_update_and_remove_of_a_placed_name_are_400_not_direct_naming_the_placer(tmp_path):
    src = tmp_path / "src"
    c = _package(src, "c")
    a = _package(src, "a", deps_={"c": _dep(c)})
    srv, cl = _server(tmp_path)
    prj = _format2(srv.store.create("p"))
    assert cl.post("/api/p/p/deps", json={"name": "alib", "git": str(a), "rev": "v1.0.0"}).status_code == 200
    before = _files(prj)
    for r in (cl.post("/api/p/p/deps/c/update", json={}), cl.post("/api/p/p/deps/c/remove")):
        assert r.status_code == 400, r.text
        body = r.json()
        assert body["code"] == "not_direct" and body["placed_by"] == ["alib"]
        assert body["detail"] == "c: placed by alib; update or remove that instead"
    assert _files(prj) == before

    r = cl.post("/api/p/p/deps/alib/update", json={})
    assert r.status_code == 200 and r.json()["packages_changed"] == [], r.text
    r = cl.post("/api/p/p/deps/alib/remove")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["kept"] == []
    assert sorted(x["package"] for x in body["removed_packages"]) == ["a", "c"]
    assert not deps.dep_dir(prj.path, "c").exists()


@needs_git
def test_add_on_a_fresh_clone_with_a_package_locked_is_409_not_synced(tmp_path, monkeypatch):
    src = tmp_path / "src"
    c = _package(src, "c")
    a = _package(src, "a", deps_={"c": _dep(c)})
    plain = src / "plain"
    _put(plain, {"inv.sym": "v {}\nK {type=subcircuit}\n"}, "v1")
    srv, cl = _server(tmp_path)
    prj = _format2(srv.store.create("p"))
    deps.add(srv.project("p"), "a", str(a), "v1.0.0")
    shutil.rmtree(prj.path / deps.DEPS)
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "empty-store"))
    before = _files(prj)
    r = cl.post("/api/p/p/deps", json={"name": "plain", "git": str(plain), "rev": "v1"})
    assert r.status_code == 409 and r.json()["code"] == "not_synced", r.text
    assert r.json()["detail"].startswith("a: not in the store or installed: run lib sync first")
    assert _files(prj) == before


def test_the_new_refusals_have_their_codes_and_keep_the_message_as_detail():
    for e, status, code in ((deps.NeedsFormat2("ip: package ip names dependencies (c): needs lock format 2"), 409,
                             "needs_lock_format_2"),
                            (locked.NotAvailable("ip: not in the store or installed: run lib sync first"), 409,
                             "not_synced")):
        h = _err(e)
        assert (h.status_code, h.code, h.detail) == (status, code, str(e))


# ------------------------------------------------------------------ trust


@needs_git
def test_trust_needs_a_fresh_trust_token_bound_to_the_key_and_the_installed_tree(tmp_path):
    repo = tmp_path / "src" / "ip"
    _put(repo, {"inv.sch": CONTROL_SCH.replace("hello", "one"), "inv.sym": "v {}\nK {type=subcircuit}\n",
                MANIFEST: _manifest("analog_ip", "1.2.0")}, "v1")
    _put(repo, {"inv.sch": CONTROL_SCH.replace("hello", "two")}, "v2")       # other content, the same version
    srv, cl = _server(tmp_path)
    prj = _format2(srv.store.create("p"))
    deps.add(prj, "analog_ip", str(repo), "v1")

    listing = cl.get("/api/p/p/trust").json()
    assert listing["trusted"] == []
    [row] = listing["dependencies"]
    assert {k: row[k] for k in ("name", "package", "version", "key", "trusted")} == \
        {"name": "analog_ip", "package": "analog_ip", "version": "1.2.0", "key": "analog_ip@1.2.0", "trusted": False}
    assert {(f["content"], f["cell"], f["file"], f["kind"]) for f in row["findings"]} >= \
        {("spice", "analog_ip/inv", "analog_ip/inv.sch", "control")}

    def trust_(token=None, name="analog_ip", project="p"):
        return cl.post(f"/api/p/{project}/trust", json={"name": name, "token": token})

    def confirm(name="analog_ip", project="p"):
        return cl.post("/api/confirmations", json={"kind": "trust", "project": project, "name": name})

    r = trust_()
    assert r.status_code == 403 and r.json()["detail"] == \
        "trusting a dependency needs the person's confirmation: no token was given"
    assert trust_("made-up").status_code == 403
    pdk_token, _ = srv.confirmations.issue({"kind": "pdk", "project": "p"})
    r = trust_(pdk_token)
    assert r.status_code == 403 and "for a pdk fetch, not trusting a dependency" in r.json()["detail"]
    other, _ = srv.confirmations.issue({"kind": "trust", "project": "q", "name": "analog_ip",
                                        "key": "analog_ip@1.2.0", "tree": "x"})
    assert trust_(other).status_code == 403
    assert confirm(project="p~0123456789ab").status_code == 400            # a preview
    assert confirm(name="nope").status_code == 404
    assert confirm(name="../x").status_code == 400

    # the token names the key and the tree it was issued for; a name it was not issued for is refused
    r = confirm()
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["key"] == "analog_ip@1.2.0" and got["tree"] == deps.tree_sha(deps.dep_dir(prj.path, "analog_ip"))
    assert got["findings"] == row["findings"]
    assert trust_(got["token"], name="other").status_code == 403

    # another tree under the same version since the confirmation: refused, nothing written
    tok = confirm().json()["token"]
    deps.update(srv.project("p"), "analog_ip", "v2")
    r = trust_(tok)
    assert r.status_code == 403 and "changed since it was confirmed (tree: " in r.json()["detail"], r.text
    assert "analog_ip@1.2.0 then" not in r.json()["detail"]                 # the key did not change: not shown
    assert "trusted" not in _meta(prj)

    tok = confirm().json()["token"]
    r = cl.post("/api/p/p~0123456789ab/trust", json={"name": "analog_ip", "token": tok})
    assert r.status_code == 400 and "changeset preview" in r.json()["detail"]
    r = trust_(tok)                                                          # the preview did not use it up
    assert r.status_code == 200 and r.json() == {"key": "analog_ip@1.2.0", "trusted": True}, r.text
    assert _meta(prj)["trusted"] == ["analog_ip@1.2.0"]
    assert cl.get("/api/p/p/trust").json()["dependencies"][0]["trusted"] is True
    r = trust_(tok)
    assert r.status_code == 403 and "used already" in r.json()["detail"]

    # taking trust back needs no token
    r = cl.post("/api/p/p/trust", json={"name": "analog_ip", "untrust": True})
    assert r.status_code == 200 and r.json() == {"key": "analog_ip@1.2.0", "trusted": False}
    assert "trusted" not in _meta(prj)


def test_a_pdk_tokens_messages_are_phase_1s():
    cf = fetchjobs.Confirmations()
    with pytest.raises(PermissionError) as e:
        cf.take(None, "pdk")
    assert str(e.value) == "a pdk fetch needs the person's confirmation: no token was given"
    with pytest.raises(PermissionError) as e:
        cf.take("nope", "pdk")
    assert str(e.value) == "the confirmation token is unknown or was used already: confirm the fetch again"
    tok, _ = cf.issue({"kind": "trust"})
    with pytest.raises(PermissionError) as e:
        cf.take(tok, "trust")
        cf.take(tok, "trust")
    assert str(e.value) == "the confirmation token is unknown or was used already: confirm the trust again"


# ------------------------------------------------------------------ [exports]


@needs_git
def test_symbols_leave_out_internal_cells_and_the_cell_list_marks_them(tmp_path):
    src = tmp_path / "src"
    ip = _package(src, "analog_ip", "1.2.0", cells=("pub", "priv"), body='\n[exports]\ncells = ["pub"]\n')
    srv, cl = _server(tmp_path)
    prj = _format2(srv.store.create("p"))
    prj.create_library("mylib")
    (prj.path / "libs" / "mylib" / "mine.sym").write_text("v {}\nK {type=subcircuit}\n")
    assert cl.post("/api/p/p/deps", json={"name": "analog_ip", "git": str(ip), "rev": "v1.2.0"}).status_code == 200

    refs = {s["ref"] for s in cl.get("/api/p/p/symbols").json()}
    assert {"analog_ip/pub.sym", "mylib/mine.sym"} <= refs and "analog_ip/priv.sym" not in refs
    cells = {x["name"]: x for x in cl.get("/api/p/p/l/analog_ip/cells").json()}
    assert cells["priv"].get("internal") is True and not cells["pub"].get("internal")
    assert (deps.dep_dir(prj.path, "analog_ip") / "priv.sym").is_file()     # installed all the same


# ------------------------------------------------------------------ set-pdk


def _pdks(root: Path) -> dict:
    for d in ("a", "b"):
        (root / d / "libs.tech").mkdir(parents=True, exist_ok=True)
        (root / d / "libs.tech" / "models.spice").write_text(f"* {d}\n")
    return {p.id: p for p in (Pdk("fakeA", "fake", A, root / "a"), Pdk("fakeB", "fake", B, root / "b"))}


def test_post_pdk_to_a_pdk_a_locked_package_does_not_accept_is_409_with_the_packages(tmp_path):
    srv, cl = _server(tmp_path, _pdks(tmp_path))
    prj = srv.store.create("p", "fakeA")
    pdkops.upgrade(prj)
    man = b'[package]\nname = "analog_ip"\nversion = "1.2.0"\n[pdk]\nfamily = "fake"\nvariants = ["fakeA", "fakeB"]\n'
    man += f'commits = ["{A}"]\n'.encode()
    files = {"comparator.sym": ("100644", cas.put_bytes(b"v {}\n")), MANIFEST: ("100644", cas.put_bytes(man))}
    meta = _meta(prj)
    meta["libraries"]["ip"] = {"git": "https://example.org/ip.git", "rev": "v1.2.0"}
    (prj.path / "project.json").write_text(json.dumps(meta, indent=2) + "\n")
    lock = deps.read_lock(prj.path)
    lock["libraries"]["ip"] = {"git": "https://example.org/ip.git", "rev": "v1.2.0", "commit": "2" * 40,
                               "tree": cas.write_tree(files), "files": "include", "package": "analog_ip",
                               "version": "1.2.0", "names": ["ip", "analog_ip"], "deps": {}}
    deps.write_lock(prj.path, lock)
    before = _files(prj)

    r = cl.post("/api/p/p/pdk", json={"pdk": "fakeB"})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "pdk_incompatible" and [p["name"] for p in body["packages"]] == ["ip"]
    assert body["packages"][0]["reason"] == "fakeB@bbbbbbbbbbbb is not among commits aaaaaaaaaaaa"
    with pytest.raises(compat.IncompatiblePdk) as e:
        srv.project("p").set_pdk("fakeB")
    assert body["detail"] == str(e.value) and _files(prj) == before
