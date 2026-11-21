"""Phase 3 of docs/design/packages.md: licences and vendoring (store/vendor.py). `pkg vendor` writes every locked tree
with vendor.json and the licences; `pkg sync --from-vendor` installs the project again from that copy with the
package repositories deleted, the store emptied and no network; a vendored file changed by hand is refused; the
tapeout bundle carries vendor/ and report.json lists it. Every repository here is local, under the test's temporary
directory."""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_pkg_index import World, _commit, _git, _project  # noqa: E402
from undine import pkgcli  # noqa: E402
from undine.store import cas, deps, vendor  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def _setup(tmp_path: Path):
    """A project with a package from the index (op 1.0.0), a package from git whose manifest gives its licence (amp
    1.0.0, with an executable script) and a plain git library with a LICENSE file (ip)."""
    w = World(tmp_path)
    w.default()
    w.publish("op", "1.0.0")
    amp = tmp_path / "repos" / "amp"
    _commit(amp, {"undine-package.toml": '[package]\nname = "amp"\nversion = "1.0.0"\nlicense = "MIT"\n',
                  "amp.sym": "v {}\nK {type=subcircuit}\n", "tools/gen.sh": "#!/bin/sh\necho hi\n"}, "amp", "v1.0.0")
    _git(amp, "update-index", "--chmod=+x", "tools/gen.sh")
    _git(amp, "commit", "-qm", "exec")
    _git(amp, "tag", "-f", "v1.0.0")
    ip = tmp_path / "repos" / "ip"
    _commit(ip, {"ip_cell.sym": "v {}\n", "LICENSE": "Apache License 2.0\n", "sub/COPYING.txt": "not top level\n"},
            "ip", "v1")
    store, prj = _project(tmp_path)
    deps.add(prj, "op", None, package="op")
    deps.add(prj, "amp", str(amp), rev="v1.0.0")
    deps.add(prj, "ip", str(ip), rev="v1")
    return w, store, prj


def _installed_files(prj) -> dict:
    out = {}
    for name in ("op", "amp", "ip"):
        d = deps.dep_dir(prj.path, name)
        out[name] = {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*"))
                     if p.is_file() and p.name != deps.MARKER}
    return out


def test_licenses_come_from_the_manifest_the_index_and_licence_files(tmp_path):
    _, _, prj = _setup(tmp_path)
    data = vendor.licenses(prj)
    rows = {r["name"]: r for r in data["entries"]}
    assert rows["amp"]["license"] == "MIT" and rows["amp"]["license_from"] == "undine-package.toml"
    assert rows["op"]["license"] == "Apache-2.0" and rows["op"]["license_from"] == "index"
    assert rows["ip"]["license"] is None and rows["ip"]["license_files"] == ["LICENSE"]
    assert rows["op"]["dir"] == "op@1.0.0" and rows["amp"]["dir"] == "amp@1.0.0"
    assert rows["ip"]["dir"] == f"ip@{rows['ip']['commit']}"
    assert data["pdk"] is None
    md = vendor.licenses_md(data)
    assert "| amp | 1.0.0 | MIT | undine-package.toml | - |" in md and "| ip | git " in md and "LICENSE |" in md


def test_pdk_licence_is_recorded_from_the_lock(tmp_path):
    _, prj = _project(tmp_path)
    lock = deps.read_lock(prj.path)
    lock["pdk"] = {"id": "ihp-sg13g2@0123456789ab", "commit": "0123456789ab" + "0" * 28}
    deps.write_lock(prj.path, lock)
    p = vendor.licenses(prj)["pdk"]
    assert p["family"] == "ihp-sg13" and p["license"] == "Apache-2.0" and p["commit"].startswith("0123456789ab")


def test_vendor_then_sync_from_vendor_with_the_repositories_and_store_gone(tmp_path, monkeypatch):
    w, store, prj = _setup(tmp_path)
    before = _installed_files(prj)
    lock = deps.read_lock(prj.path)
    out = tmp_path / "bundle"
    r = vendor.vendor(prj, out)
    v = out / "vendor"
    want = ["op@1.0.0", "amp@1.0.0", f"ip@{lock['libraries']['ip']['commit']}"]
    assert sorted(x["dir"] for x in r["entries"]) == sorted(want)
    meta = json.loads((v / "vendor.json").read_text())
    assert meta["entries"]["amp@1.0.0"]["tree"] == lock["libraries"]["amp"]["tree"]
    assert meta["lock"]["format"] == 2 and len(meta["lock"]["sha256"]) == 64
    assert os.stat(v / "amp@1.0.0" / "tools" / "gen.sh").st_mode & 0o777 == 0o755
    assert os.stat(v / "amp@1.0.0" / "amp.sym").st_mode & 0o777 == 0o644
    assert json.loads((v / "LICENSES.json").read_text())["project"] == "p" and (v / "LICENSES.md").is_file()
    assert not list(out.glob(".vendor.new*"))
    # the repositories deleted, the store emptied, the installs removed; no network
    shutil.rmtree(tmp_path / "repos")
    shutil.rmtree(w.repos)
    shutil.rmtree(cas.store_root())
    shutil.rmtree(prj.path / deps.DEPS)
    monkeypatch.setenv("UNDINE_OFFLINE", "1")
    with pytest.raises(deps.DepError, match="offline, and not in the store"):
        deps.sync(prj, offline=True)
    res = vendor.sync_from_vendor(prj, out)              # the bundle, or its vendor/
    assert {x["name"]: x["action"] for x in res["imported"]} == {"op": "imported", "amp": "imported", "ip": "imported"}
    assert {x["name"]: x["action"] for x in res["sync"]} == {"op": "installed", "amp": "installed", "ip": "installed"}
    assert _installed_files(prj) == before
    assert deps.read_lock(prj.path) == lock
    again = vendor.import_vendor(prj, v)
    assert {x["action"] for x in again} == {"in store"}


def test_a_vendored_file_changed_by_hand_is_refused_and_nothing_imported(tmp_path):
    _, _, prj = _setup(tmp_path)
    out = tmp_path / "bundle"
    vendor.vendor(prj, out)
    shutil.rmtree(cas.store_root())
    shutil.rmtree(prj.path / deps.DEPS)
    (out / "vendor" / "amp@1.0.0" / "amp.sym").write_text("changed\n")
    with pytest.raises(vendor.VendorError, match="amp@1.0.0: its files do not hash to the lock's tree"):
        vendor.sync_from_vendor(prj, out)
    assert not (cas.store_root() / "trees").exists() and not (cas.store_root() / "objects").exists()
    (out / "vendor" / "amp@1.0.0" / "amp.sym").write_text("v {}\nK {type=subcircuit}\n")
    (out / "vendor" / "amp@1.0.0" / "extra.txt").write_text("added\n")
    with pytest.raises(vendor.VendorError, match="changed, added or removed"):
        vendor.import_vendor(prj, out)
    os.unlink(out / "vendor" / "amp@1.0.0" / "extra.txt")
    os.symlink("/etc/passwd", out / "vendor" / "amp@1.0.0" / "link")
    with pytest.raises(vendor.VendorError, match="not a regular file"):
        vendor.import_vendor(prj, out)


def test_a_tree_not_in_the_store_is_fetched_only_when_asked(tmp_path):
    _, _, prj = _setup(tmp_path)
    shutil.rmtree(cas.store_root() / "trees")
    shutil.rmtree(prj.path / deps.DEPS)
    out = tmp_path / "bundle"
    with pytest.raises(vendor.VendorError) as e:
        vendor.vendor(prj, out)
    assert "not in the store or installed: amp 1.0.0, ip @" in str(e.value) and "op 1.0.0" in str(e.value)
    assert "nothing was written" in str(e.value) and not (out / "vendor").exists()
    asked = []
    with pytest.raises(vendor.VendorError):
        vendor.vendor(prj, out, offline=False, confirm=lambda names: asked.append(names) or False)
    assert len(asked[0]) == 3 and not (out / "vendor").exists()
    r = vendor.vendor(prj, out, offline=False, confirm=lambda names: True)       # from the kept repositories
    assert len(r["entries"]) == 3 and (out / "vendor" / "vendor.json").is_file()
    with pytest.raises(vendor.VendorError, match="exists: remove it"):
        vendor.vendor(prj, out)


def test_an_install_as_locked_is_vendored_without_the_store(tmp_path):
    _, _, prj = _setup(tmp_path)
    shutil.rmtree(cas.store_root() / "trees")             # no record: the verified installs are read
    r = vendor.vendor(prj, tmp_path / "b")
    assert len(r["entries"]) == 3


def test_the_cli_vendors_lists_licences_and_syncs_from_vendor(tmp_path, capsys):
    _, store, prj = _setup(tmp_path)
    parser = __import__("argparse").ArgumentParser()
    pkgcli.add_parser(parser.add_subparsers(dest="cmd"))

    def run(*argv) -> int:
        return pkgcli.run(parser.parse_args(["pkg", *argv]), ProjectStore(store.root, {}))
    assert run("licenses", "p") == 0
    out = capsys.readouterr().out
    assert "MIT" in out and "Apache-2.0" in out and "LICENSE" in out
    assert run("licenses", "p", "--json") == 0
    assert json.loads(capsys.readouterr().out)["project"] == "p"
    assert run("vendor", "p", str(tmp_path / "b")) == 0
    assert "wrote" in capsys.readouterr().out
    assert run("vendor", "p", str(tmp_path / "b")) == 1
    assert "exists" in capsys.readouterr().out
    shutil.rmtree(cas.store_root())
    shutil.rmtree(prj.path / deps.DEPS)
    assert run("sync", "p", "--from-vendor", str(tmp_path / "b")) == 0
    out = capsys.readouterr().out
    assert "amp: imported from vendor/amp@1.0.0" in out and "amp: installed" in out


def test_the_tapeout_bundle_has_vendor_and_report_lists_it(tmp_path):
    from test_tapeout import PDK, P, tile
    from undine.checks import tapeout
    _, _, prj = _setup(tmp_path)
    r = tapeout.export_bundle(prj.path, tile(), "lib", "mycell", PDK, "tt-sky130-analog", P, "rev1",
                              vendor=lambda d: vendor.vendor(prj, d, offline=True))
    d = prj.path / r["dir"]
    rep = json.loads((d / "report.json").read_text())
    assert sorted(e["dir"] for e in rep["vendor"]["entries"])[0] == "amp@1.0.0"
    assert {x["name"]: x["license"] for x in rep["vendor"]["licenses"]}["amp"] == "MIT"
    assert "vendor/vendor.json" in rep["files"] and "vendor/LICENSES.md" in rep["files"]
    assert any(f.startswith("vendor/amp@1.0.0/") for f in r["files"])
    # a failing vendor still writes the bundle, and says why
    r2 = tapeout.export_bundle(prj.path, tile(), "lib", "mycell", PDK, "tt-sky130-analog",
                               P | {"top_module": "tt_um_other"}, "rev1",
                               vendor=lambda d: (_ for _ in ()).throw(vendor.VendorError("not in the store: x")))
    rep2 = json.loads((prj.path / r2["dir"] / "report.json").read_text())
    assert rep2["vendor"] == {"error": "not in the store: x"}
    assert any(c["id"] == "vendor" and c["status"] == "warn" for c in rep2["checks"])
